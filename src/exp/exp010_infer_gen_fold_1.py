# =========================
# libraries
# =========================
import json
import numpy as np
import pandas as pd
from tqdm import tqdm
from pathlib import Path
import time
import logging
from contextlib import contextmanager
import sys
from transformers import AutoTokenizer
from transformers import get_linear_schedule_with_warmup
from torch.optim import AdamW
from torch.cuda.amp import autocast, GradScaler
import torch.nn as nn
import torch
from torch.utils.data import DataLoader, Dataset
from sklearn.model_selection import GroupKFold
import random
import os
from cuml.neighbors import NearestNeighbors
from transformers import BitsAndBytesConfig
from torch import Tensor
from peft import LoraConfig, get_peft_model
from typing import List, Optional
from transformers import Qwen2Model, Qwen2ForCausalLM
os.environ["TOKENIZERS_PARALLELISM"] = "true"

# =========================
# constants
# =========================

HOME_PATH = Path(os.environ["HOME"]) # Change this to your home directory
DATA_DIR = HOME_PATH / Path("data/eedi-mining-misconceptions-in-mathematics")
OUTPUT_DIR = HOME_PATH / Path("results")
TRAIN_PATH = OUTPUT_DIR / Path("train_gen/train_gen_8k.csv")
MISCONCEPTION_MAPPING_PATH = DATA_DIR / "misconception_mapping.csv"
LLM_TEXT_PATH = OUTPUT_DIR / Path("exp105_train_gen_8k_add_text.csv")
FOLD_PATH = HOME_PATH / "eedi_fold.csv"
FOLDS = [1]
# =========================
# settings
# =========================
exp = f"010_infer_gen_fold_{FOLDS[0]}"
exp_base = f"010_fold_{FOLDS[0]}"
exp_dir = OUTPUT_DIR / "exp" / f"ex{exp}"
model_dir = OUTPUT_DIR / "exp" / f"ex{exp_base}" / "model"
lora_path = OUTPUT_DIR / "exp" / f"ex{exp_base}" / "model" / f"fold{FOLDS[0]}" / "adapter.bin"

exp_dir.mkdir(parents=True, exist_ok=True)
logger_path = exp_dir / f"ex{exp}.txt"
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# =========================
# mdoel settings
# =========================

seed = 1
model_path = "Qwen/Qwen2.5-32B-Instruct-GPTQ-Int4"
batch_size = 64
negative_size = 96
n_epochs = 1
max_len = 384
weight_decay = 0.1
lr = 1e-4
num_warmup_steps_rate = 0.1
tokenizer = AutoTokenizer.from_pretrained(model_path)
n_candidate = 50
iters_to_accumulate = 1

# ===============
# Functions
# ===============


def set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


class EediValDataset(Dataset):
    def __init__(self, text1,
                 tokenizer, max_len):
        self.text1 = text1
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self):
        return len(self.text1)

    def __getitem__(self, item):
        text1 = self.text1[item]
        inputs1 = self.tokenizer(
            text1,
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_attention_mask=True,
            return_token_type_ids=True
        )
        inputs1 = {"input_ids": torch.tensor(inputs1["input_ids"], dtype=torch.long),
                   "attention_mask": torch.tensor(inputs1["attention_mask"],
                                                  dtype=torch.long),
                   "token_type_ids": torch.tensor(inputs1["token_type_ids"],
                                                  dtype=torch.long)}

        return inputs1


class Qwen2ModelLabel(Qwen2ForCausalLM):
    def __init__(self, config):
        super().__init__(config)

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        labels: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
    ):
        return self.model.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            cache_position=cache_position,
        )


class BiEncoderModel(nn.Module):
    def __init__(self,
                 sentence_pooling_method: str = "last"
                 ):
        super().__init__()
        model = Qwen2ModelLabel.from_pretrained(model_path)
        model.enable_input_require_grads()
        # model = IgnoreLabelsWrapper(model)
        config = LoraConfig(
            r=32,
            lora_alpha=64,
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
            bias="none",
            lora_dropout=0.05,  # Conventional
            task_type="CAUSAL_LM",
        )
        self.model = get_peft_model(model, config)
        d = torch.load(lora_path, map_location=model.device)
        print(f"load model from {lora_path}")
        self.model.load_state_dict(d, strict=False)
        self.model.print_trainable_parameters()
        self.sentence_pooling_method = sentence_pooling_method
        self.model.config.use_cache = False
        self.config = self.model.config

    def gradient_checkpointing_enable(self, **kwargs):
        self.model.gradient_checkpointing_enable(**kwargs)

    def last_token_pool(self, last_hidden_states: Tensor,
                        attention_mask: Tensor) -> Tensor:
        left_padding = (attention_mask[:, -1].sum() == attention_mask.shape[0])
        if left_padding:
            return last_hidden_states[:, -1]
        else:
            sequence_lengths = attention_mask.sum(dim=1) - 1
            batch_size = last_hidden_states.shape[0]
            return last_hidden_states[torch.arange(batch_size, device=last_hidden_states.device), sequence_lengths]

    def sentence_embedding(self, hidden_state, mask):
        if self.sentence_pooling_method == 'mean':
            s = torch.sum(hidden_state * mask.unsqueeze(-1).float(), dim=1)
            d = mask.sum(axis=1, keepdim=True).float()
            return s / d
        elif self.sentence_pooling_method == 'cls':
            return hidden_state[:, 0]
        elif self.sentence_pooling_method == 'last':
            return self.last_token_pool(hidden_state, mask)

    def encode(self, input_is, attention_mask):
        # print(features)
        psg_out = self.model(input_ids=input_is, attention_mask=attention_mask,
                             return_dict=True)
        p_reps = self.sentence_embedding(
            psg_out.last_hidden_state, attention_mask)
        return p_reps.contiguous()

    def forward(self, input_is, attention_mask):
        q_reps = self.encode(input_is, attention_mask)
        return q_reps

    def _dist_gather_tensor(self, t: Optional[torch.Tensor]):
        if t is None:
            return None
        t = t.contiguous()

        all_tensors = [torch.empty_like(t) for _ in range(self.world_size)]
        dist.all_gather(all_tensors, t)

        all_tensors[self.process_rank] = t
        all_tensors = torch.cat(all_tensors, dim=0)

        return all_tensors


def get_optimizer_grouped_parameters(
        model,
        weight_decay,
        lora_lr=5e-4,
        no_decay_name_list=["bias", "LayerNorm.weight"],
        lora_name_list=["lora_right_weight", "lora_left_weight"],
):
    optimizer_grouped_parameters = [
        {
            "params": [
                p for n, p in model.named_parameters()
                if (not any(nd in n for nd in no_decay_name_list)
                    and p.requires_grad and not any(nd in n
                                                    for nd in lora_name_list))
            ],
            "weight_decay":
                weight_decay,
        },
        {
            "params": [
                p for n, p in model.named_parameters()
                if (not any(nd in n for nd in no_decay_name_list)
                    and p.requires_grad and any(nd in n
                                                for nd in lora_name_list))
            ],
            "weight_decay":
                weight_decay,
            "lr":
                lora_lr
        },
        {
            "params": [
                p for n, p in model.named_parameters()
                if (any(nd in n
                        for nd in no_decay_name_list) and p.requires_grad)
            ],
            "weight_decay":
                0.0,
        },
    ]
    if not optimizer_grouped_parameters[1]["params"]:
        optimizer_grouped_parameters.pop(1)
    return optimizer_grouped_parameters




def get_detailed_instruct(task_description: str, query: str) -> str:
    return f'Instruct: {task_description}\nQuery: {query}'


task = 'Given a math problem statement and an incorrect answer as a query, retrieve relevant passages that identify and explain the nature of the error.'


def cos_sim(a, b):
    # From https://github.com/UKPLab/sentence-transformers/blob/master/
    # sentence_transformers/util.py#L31
    """
    Computes the cosine similarity cos_sim(a[i], b[j]) for all i and j.
    :return: Matrix with res[i][j]  = cos_sim(a[i], b[j])
    """
    if not isinstance(a, torch.Tensor):
        a = torch.tensor(a)

    if not isinstance(b, torch.Tensor):
        b = torch.tensor(b)

    if len(a.shape) == 1:
        a = a.unsqueeze(0)

    if len(b.shape) == 1:
        b = b.unsqueeze(0)

    a_norm = torch.nn.functional.normalize(a, p=2, dim=1)
    b_norm = torch.nn.functional.normalize(b, p=2, dim=1)
    return torch.mm(a_norm, b_norm.transpose(0, 1))


def collate_sentence(d):
    mask_len = int(d["attention_mask"].sum(axis=1).max())
    return {"input_ids": d['input_ids'][:, :mask_len],
            "attention_mask": d['attention_mask'][:, :mask_len]}


def make_candidate_first_stage_for_val(val, misconception,
                                       model, tokenizer, max_len,
                                       batch_size, n_neighbor=100):
    val_ = EediValDataset(val["all_text"],
                          tokenizer,
                          max_len)
    misconception_ = EediValDataset(misconception["MisconceptionName"],
                                    tokenizer,
                                    max_len)

    print("make val emb")
    val_loader = DataLoader(
        val_, batch_size=batch_size * 2, shuffle=False)
    val_emb = make_emb(model, val_loader)

    print("make misconception emb")
    misconcept_loader = DataLoader(
        misconception_, batch_size=batch_size * 2, shuffle=False)
    misconcept_emb = make_emb(model, misconcept_loader)

    print("running knn")
    knn = NearestNeighbors(n_neighbors=n_neighbor,
                           metric="cosine")
    knn.fit(misconcept_emb)
    dists, nears = knn.kneighbors(val_emb)
    print("knn done")
    return nears


def make_emb(model, train_loader):
    bert_emb = []
    with torch.no_grad():
        for d in tqdm(train_loader):
            d = collate_sentence(d)
            input_ids = d['input_ids']
            mask = d['attention_mask']
            input_ids = input_ids.to(device)
            mask = mask.to(device)
            output = model(input_ids, mask)
            output = output.detach().cpu().numpy().astype(np.float32)
            bert_emb.append(output)
    torch.cuda.empty_cache()
    bert_emb = np.concatenate(bert_emb)
    return bert_emb


def calculate_map25_with_metrics(df):
    def ap_at_k(actual, predicted, k=25):
        actual = int(actual)
        predicted = predicted[:k]
        score = 0.0
        num_hits = 0.0
        found = False
        rank = None
        for i, p in enumerate(predicted):
            if p == actual:
                if not found:
                    found = True
                    rank = i + 1
                num_hits += 1
                score += num_hits / (i + 1.0)
        return score, found, rank

    scores = []
    found_count = 0
    rankings = []
    total_count = 0

    for _, row in df.iterrows():
        actual = row['MisconceptionId']
        predicted = [int(float(x)) for x in row['pred'].split()]
        score, found, rank = ap_at_k(actual, predicted)
        scores.append(score)

        total_count += 1
        if found:
            found_count += 1
            rankings.append(rank)

    map25 = np.mean(scores)
    percent_found = (found_count / total_count) * 100 if total_count > 0 else 0
    avg_ranking = np.mean(rankings) if rankings else 0

    return map25, percent_found, avg_ranking


class EediTrainDataset(Dataset):
    def __init__(self, querys, misconception,
                 tokenizer, max_len):
        self.querys = querys
        self.misconception = misconception
        self.tokenizer = tokenizer
        self.max_len = max_len

    def __len__(self):
        return len(self.querys)

    def __getitem__(self, item):
        query = self.querys[item]
        misconception = self.misconception[item]
        inputs1 = self.tokenizer(
            query,
            max_length=self.max_len,
            padding="max_length",
            truncation=True,
            return_attention_mask=True,
            return_token_type_ids=True
        )

        inputs1 = {"input_ids": torch.tensor(inputs1["input_ids"],
                                             dtype=torch.long),
                   "attention_mask": torch.tensor(inputs1["attention_mask"],
                                                  dtype=torch.long)}
        return {"inputs1": inputs1, "misconception": misconception}


def compute_similarity(q_reps, p_reps):
    if len(p_reps.size()) == 2:
        return torch.matmul(q_reps, p_reps.transpose(0, 1))
    return torch.matmul(q_reps, p_reps.transpose(-2, -1))


LOGGER = logging.getLogger()
FORMATTER = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")


def setup_logger(out_file=None, stderr=True,
                 stderr_level=logging.INFO, file_level=logging.DEBUG):
    LOGGER.handlers = []
    LOGGER.setLevel(min(stderr_level, file_level))

    if stderr:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(FORMATTER)
        handler.setLevel(stderr_level)
        LOGGER.addHandler(handler)

    if out_file is not None:
        handler = logging.FileHandler(out_file)
        handler.setFormatter(FORMATTER)
        handler.setLevel(file_level)
        LOGGER.addHandler(handler)

    LOGGER.info("logger set up")
    return LOGGER


@ contextmanager
def timer(name):
    t0 = time.time()
    yield
    LOGGER.info(f'[{name}] done in {time.time() - t0:.0f} s')


setup_logger(out_file=logger_path)


# ============================
# main
# ============================
train = pd.read_csv(TRAIN_PATH)
train['QuestionId'] = np.arange(len(train)) + 100000
train['ConstructId'] = np.arange(len(train)) + 100000
train['SubjectId'] = np.arange(len(train)) + 100000
misconception = pd.read_csv(MISCONCEPTION_MAPPING_PATH)
misconception_ids = []
for s in "ABCD":
    misconception_ids += [int(x) for x in train[f"Misconception{s}Id"].values if not np.isnan(x)]
misconception_ids = list(set(misconception_ids))
misconception = misconception[misconception["MisconceptionId"].isin(misconception_ids)].reset_index(drop=True)
llm_text = pd.read_csv(LLM_TEXT_PATH)
df_fold = pd.read_csv(FOLD_PATH)

train_pivot = []
common_cols = ['QuestionId', 'ConstructId', 'ConstructName', 'SubjectId',
               'SubjectName', 'CorrectAnswer', 'QuestionText']
for i in ["A", "B", "C", "D"]:
    train_ = train.copy()
    train_ = train[common_cols + [f"Answer{i}Text", f"Misconception{i}Id"]]
    train_ = train_.rename({f"Answer{i}Text": "AnswerText",
                            f"Misconception{i}Id": "MisconceptionId"}, axis=1)
    train_["ans"] = i
    train_pivot.append(train_)

train_pivot = pd.concat(train_pivot).reset_index(drop=True)
train_pivot_correct_ans = train_pivot[
    train_pivot["CorrectAnswer"] == train_pivot["ans"]].reset_index(drop=True)
train_pivot_correct_ans = train_pivot_correct_ans[[
    "QuestionId", "AnswerText"]].reset_index(drop=True)
train_pivot_correct_ans.columns = ["QuestionId", "CorrectAnswerText"]
train_pivot = train_pivot[train_pivot["MisconceptionId"].notnull()].reset_index(
    drop=True)

train_pivot = train_pivot.merge(
    llm_text[["QuestionId", "ans", "llmMisconception"]], how="left", on=["QuestionId", "ans"])
train_pivot = train_pivot.merge(
    train_pivot_correct_ans, how="left", on="QuestionId")

train_pivot["all_text"] = ' <Question> ' + train_pivot['QuestionText'] + \
    ' <Correct Answer> ' + train_pivot['CorrectAnswerText'] + \
    ' <Incorrect Answer> ' + train_pivot['AnswerText'] + \
    ' <Construct> ' + train_pivot['ConstructName'] + \
    ' <Subject> ' + train_pivot['SubjectName'] + \
    ' <LLMOutput> ' + train_pivot['llmMisconception']
train_pivot["MisconceptionId"] = train_pivot["MisconceptionId"].astype(int)
train_pivot = train_pivot.merge(
    misconception, how="left", on="MisconceptionId")

text_list = []
for t in train_pivot["all_text"].values:
    text_list.append(get_detailed_instruct(task, t))
train_pivot["all_text"] = text_list
df_fold = df_fold.drop_duplicates(subset=["QuestionId"]).reset_index(drop=True)
# train_pivot = train_pivot.merge(
#     df_fold[["QuestionId", "fold"]], how="left", on="QuestionId")
train_pivot["fold"] = -1
fold_array = train_pivot["fold"].values

# ================================
# train
# ================================
# ================================
# train
# ================================
with timer("train"):
    set_seed(seed)
    gkf = GroupKFold(n_splits=5)
    val_pred_all = []
    for n in FOLDS:
        misconception_name = misconception["MisconceptionName"].values
        x_train = train_pivot[fold_array != n].reset_index(drop=True)
        negative_misconception = x_train["MisconceptionId"].unique()
        x_val = train_pivot[fold_array == -1].reset_index(drop=True)
        train_ = EediTrainDataset(x_train["all_text"],
                                  x_train["MisconceptionId"],
                                  tokenizer,
                                  max_len)

        # loader
        train_loader = DataLoader(dataset=train_,
                                  batch_size=batch_size,
                                  shuffle=True,
                                  pin_memory=True,
                                  num_workers=0)

        model = BiEncoderModel()
        model.gradient_checkpointing_enable()
        model = model.to(device)

        # optimizer, scheduler
        optimizer_grouped_parameters = get_optimizer_grouped_parameters(
            model, 0.01, 5e-4)

        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=lr,
                          betas=(0.9, 0.95),
                          fused=True)
        num_train_optimization_steps = int(len(train_loader) * n_epochs)
        num_warmup_steps = int(
            num_train_optimization_steps * num_warmup_steps_rate)
        scheduler = \
            get_linear_schedule_with_warmup(
                optimizer,
                num_warmup_steps=num_warmup_steps,
                num_training_steps=num_train_optimization_steps)

        compute_loss = nn.CrossEntropyLoss(reduction='mean')
        best_score = 0
        scaler = GradScaler()

        # val
        model.eval()
        pred = make_candidate_first_stage_for_val(x_val, misconception,
                                                    model, tokenizer, max_len*2,
                                                    batch_size, n_candidate)
        recall = 0
        for gt, p in tqdm(zip(x_val["MisconceptionId"], pred)):
            p = [misconception["MisconceptionId"].values[i] for i in p]
            if gt in p:
                recall += 1
        recall /= len(x_val)
        pred_ = []
        for p in pred:
            p = [misconception["MisconceptionId"].values[i] for i in p]
            pred_.append(' '.join(map(str, p)))

        val_pred = pd.DataFrame()
        val_pred["MisconceptionId"] = x_val["MisconceptionId"]
        val_pred["pred"] = pred_
        val_pred["QuestionId"] = x_val["QuestionId"]
        val_pred["ans"] = x_val["ans"]
        val_score, percent_found, avg_ranking = calculate_map25_with_metrics(
            val_pred)
        LOGGER.info(
            f'fold {n}: cv {val_score} recall : {recall}')
        if recall > best_score:
            best_val_pred = val_pred.copy()
            best_score = recall
        val_pred_all.append(best_val_pred)

val_pred_all = pd.concat(val_pred_all).reset_index(drop=True)
val_pred_all.to_parquet(exp_dir / f"exp{exp}_val_pred.parquet")
LOGGER.info(f'cv : {calculate_map25_with_metrics(val_pred_all)}')

