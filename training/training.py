import peft
import torch
import random
from typing import List
import bitsandbytes as bnb

from transformers import (
    Trainer,
    AutoTokenizer,
    PreTrainedModel,
    HfArgumentParser,
    BitsAndBytesConfig,
    PreTrainedTokenizer,
    AutoModelForCausalLM,
)

from training.arguments import (
    ModelArguments,
    DataArguments,
    TrainingArguments,
    TokenizerArguments
)
from dataset.algo_dataset import AlgoDataset
from dataset.packed_dataset import PackedDataset
from packing.llama_monkey_patch import LlamaForCausalLM
from packing.mistral_monkey_patch import MistralForCausalLM
from prompt_template.code_llama_template import CodellamaTemplate


def set_seed(seed: int = 100) -> None:
    random.seed(seed)
    torch.manual_seed(seed)


def config_bnb() -> BitsAndBytesConfig:
    """
    Configure bitsandbytes for QLora training
    Returns:
            BitsAndBytesConfig
    """
    # switch to float16 if device is not support bfloat16
    compute_datatype = getattr(torch, "bfloat16")
    return BitsAndBytesConfig(load_in_4bit=True,
                              bnb_4bit_quant_type="nf4",
                              bnb_4bit_use_double_quant=True,
                              bnb_4bit_compute_dtype=compute_datatype)


def find_all_linear_names(model):
    lora_module_names = set()
    for name, module in model.named_modules():
        if isinstance(module, bnb.nn.Linear4bit) or isinstance(module, torch.nn.Linear):
            names = name.split(".")
            lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    if "lm_head" in lora_module_names:  # needed for 16-bit
        lora_module_names.remove("lm_head")
    return list(lora_module_names)


def config_peft(modules: List) -> peft.LoraConfig:
    """
    Configure PEFT for QLora training
    Args:
        modules: define modules to apply Lora

    Returns:
        LoraConfig

    """
    return peft.LoraConfig(
        r=16,
        lora_alpha=64,
        target_modules=modules,
        lora_dropout=0.1,
        bias="none",
        task_type="CAUSAL_LM",
        modules_to_save=["lm_head", "embed_tokens"],
    )


def print_trainable_parameters(model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    print(
        f"Trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}")


def load_tokenizer(tokenizer_args: TokenizerArguments) -> PreTrainedTokenizer:
    """
    Loads and config pre-trained tokenizer
    Args:
        tokenizer_args:

    Returns:

    """
    tokenizer = AutoTokenizer.from_pretrained(tokenizer_args._model_name_or_path)
    tokenizer.pad_token_id = tokenizer.eos_token_id
    tokenizer.padding_side = tokenizer_args.padding_side

    if tokenizer_args.added_tokens:
        tokenizer.add_special_tokens({"additional_special_tokens": tokenizer_args.added_tokens})

    return tokenizer


def load_model(model_args: ModelArguments,
               training_args: TrainingArguments,
               tokenizer: PreTrainedTokenizer) -> PreTrainedModel:
    """
    Loads and config pre-trained model
    Args:
        model_args:
        training_args:
        tokenizer:

    Returns:

    """

    # packing data while training
    if training_args.packing:
        if model_args.model_type == "mistral":
            model_class = MistralForCausalLM
        elif model_args.model_type == "llama":
            model_class = LlamaForCausalLM
        elif model_args.model_type == "mixtral":
            model_class = LlamaForCausalLM
        else:
            model_class = MistralForCausalLM
    else:
        model_class = AutoModelForCausalLM

    quantization_config = config_bnb() if model_args.qlora else None
    model = model_class.from_pretrained(model_args.model_name_or_path,
                                        device_map={"": 0},
                                        trust_remote_code=True,
                                        use_flash_attention_2=True,
                                        quantization_config=quantization_config)

    model.resize_token_embeddings(len(tokenizer))
    model.config.pad_token_id = tokenizer.pad_token_id
    model.gradient_checkpointing_enable()

    if model_args.qlora and model_args.use_lora:
        model = peft.prepare_model_for_kbit_training(model)
    if model_args.use_lora:
        modules = find_all_linear_names(model)
        model = peft.get_peft_model(model, config_peft(modules))

    # Configure the pad token in the model
    model.config.pad_token_id = tokenizer.pad_token_id

    # Gradient checkpointing is used by default but not compatible with caching
    model.config.use_cache = False

    return model


def train():
    set_seed(100)

    arg_parser = HfArgumentParser((ModelArguments, DataArguments, TrainingArguments, TokenizerArguments))

    model_args, data_args, training_args, tokenizer_args = arg_parser.parse_args_into_dataclasses()
    print("model_args: ", model_args)
    print("data_args: ", data_args)
    print("training_args: ", training_args)
    print("tokenizer_args: ", tokenizer_args)
    tokenizer_args._model_name_or_path = model_args.model_name_or_path
    # load tokenizer
    tokenizer = load_tokenizer(tokenizer_args)
    prompt_template = CodellamaTemplate()

    train_ds = AlgoDataset(tokenizer=tokenizer,
                           data_path=data_args.train_path,
                           prompt_template=prompt_template,
                           max_seq_len=model_args.model_max_length,
                           batch_size=training_args.per_device_train_batch_size)
    valid_ds = AlgoDataset(tokenizer=tokenizer,
                           data_path=data_args.validation_path,
                           max_seq_len=model_args.model_max_length,
                           prompt_template=prompt_template,
                           batch_size=training_args.per_device_eval_batch_size)
    if training_args.packing:
        train_ds = PackedDataset(dataset=train_ds,
                                 tokenizer=tokenizer,
                                 pack_length=model_args.model_max_length)

        valid_ds = PackedDataset(dataset=valid_ds,
                                 tokenizer=tokenizer,
                                 pack_length=model_args.model_max_length)

    # model = load_model(model_args=model_args,
    #                    training_args=training_args,
    #                    tokenizer=tokenizer)
    #
    # trainer = Trainer(
    #     model=model,
    #     train_dataset=train_ds,
    #     eval_dataset=valid_ds,
    #     args=training_args,
    # )

    # trainer.train()


if __name__ == '__main__':
    train()
