# Copyright 2024 PKU-Alignment Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import argparse
import json
from align_anything.evaluation.inference.vllm_inference import *
from align_anything.evaluation.dataloader.base_dataloader import BaseDataLoader
from typing import List, Dict, Any
from datasets import load_dataset, DatasetDict
from align_anything.utils.tools import read_eval_cfgs, dict_to_namedtuple, update_dict, custom_cfgs_to_dict
from align_anything.evaluation.data_type import InferenceInput
from align_anything.evaluation.eval_logger import EvalLogger
from diffusers import StableDiffusionPipeline
from align_anything.utils.hps import get_score, get_score_single
import torch
import os

class DrawBenchDataLoader(BaseDataLoader):
    def init_tokenizer(self):
        pass

    def get_task_names(self):
        if isinstance(self.data_cfgs.task, list):
            return self.data_cfgs.task
        else:
            task_names = [
            self.data_cfgs.task
            ]
            return task_names

    def get_answer(self, data):
        return data['answer']

    def set_fewshot_dataset(self, dataset, task): 
        if self.cot:
            with open('../cot_fewshot/DrawBench/' + task + '.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        else:
            return None
    
    def build_example_prompt(self, data, with_answer=True):
        return f"{data['prompts']}"

    def build_prompt(self, data):
        prompt = ""
        cot_prompt = f" Let's think step by step. "
        few_shot_examples = self.few_shot_data[:self.num_shot] if self.num_shot else []
        if len(few_shot_examples) == 0:
            question = [prompt + self.build_example_prompt(item, False) for item in data]
        else:
            few_shots = [
                self.build_example_prompt(
                    {key: value[i] for key, value in few_shot_examples.items()}, True
                )
                for i in range(len(few_shot_examples['question']))
            ]
            question = []
            for item in data:
                request = {}
                for key, value in item.items():
                    request[key] = value
                examples = few_shots + [self.build_example_prompt(request, False)]
                if self.cot:
                    question.append(prompt + '\n\n'.join(examples) + cot_prompt)
                else:
                    question.append(prompt + '\n\n'.join(examples))
        
        return question

    def load_dataset(self) -> DatasetDict:
        processed_inputs = {}
        for task in self.task_names:
            dataset = load_dataset(self.task_dir, task)
            self.few_shot_data = self.set_fewshot_dataset(dataset, task)
            prompts = self.preprocess(dataset)
            processed_inputs[task] = [InferenceInput(text=prompt) for prompt in prompts]
        return processed_inputs

    def preprocess(self, data):
        prompts = self.build_prompt(data[self.split])
        return prompts

class DrawBenchGeneratorVLLM(BaseInferencer_vllm):
    def init_model(self) -> None:
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.pipe = get_pipeline(self.model_name_or_path).to(self.device)

    def eval(self, data:Dict[str, List[InferenceInput]], img_dir):
        task2details = {}

        if not os.path.exists(img_dir):
            os.makedirs(img_dir)
            
        for task, inputs in data.items():
            task_dir = os.path.join(img_dir, task)
            if not os.path.exists(task_dir):
                os.makedirs(task_dir)
            prompts = []
            img_paths = []
            idx = 1
            for input in tqdm(inputs, desc='Generating'):
                prompt = input.text
                image = self.pipe(prompt).images[0]
                image_path = os.path.join(task_dir, f"image_{idx}.png")
                idx += 1
                image.save(image_path)
                prompts.append(prompt)
                img_paths.append(image_path)

            task2details[task] = {
                'prompts': prompts,
                'image_paths': img_paths,
            }

        return task2details

def get_pipeline(model_name: str):
    if "stable-diffusion" in model_name:
        return StableDiffusionPipeline.from_pretrained(model_name)
    else:
        raise ValueError(f"Model '{model_name}' is not supported or unknown.")

def evaluator(raw_output, file_path):
    tot_score = 0.0
    num_sum = 0
    for prompt, img_path in tqdm(zip(raw_output['prompts'], raw_output['image_paths']), desc="Evaluating"):
        num_sum += 1
        score = float(get_score_single(img_path, prompt))
        tot_score += score
        save_detail(prompt, '', '', img_path, score, file_path)

    return tot_score / num_sum, num_sum

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _, unparsed_args = parser.parse_known_args()
    keys = [k[2:] for k in unparsed_args[0::2]]
    values = list(unparsed_args[1::2])
    unparsed_args = dict(zip(keys, values))
    logger = EvalLogger('Evaluation')
    
    dict_configs, infer_configs = read_eval_cfgs('drawbench', 'vLLM')
    
    try:
        assert dict_configs or infer_configs, "Config file does not exist or is incomplete."
    except AssertionError as e:
        logger.log('error', "Config file is not exist or incomplete.")
        exit()

    for k, v in unparsed_args.items():
        if v == '' or v is None:
            continue
        dict_configs = update_dict(dict_configs, custom_cfgs_to_dict(k, v))
        infer_configs = update_dict(infer_configs, custom_cfgs_to_dict(k, v))
    
    dict_configs, infer_configs = dict_to_namedtuple(dict_configs), dict_to_namedtuple(infer_configs)
    model_config = dict_configs.default.model_cfgs
    eval_configs = dict_configs.default.eval_cfgs
    logger.log_dir = eval_configs.output_dir
    dataloader = DrawBenchDataLoader(dict_configs)
    
    test_data = dataloader.load_dataset()
    eval_module = DrawBenchGeneratorVLLM(model_config, infer_configs)
    img_dir = f"./images/{eval_configs.uuid}"
    raw_outputs = eval_module.eval(test_data, img_dir)

    os.makedirs(logger.log_dir, exist_ok=True)
    uuid_path = f"{logger.log_dir}/{eval_configs.uuid}"
    os.makedirs(uuid_path, exist_ok=True)

    for task, _ in raw_outputs.items():
        file_path = f"{uuid_path}/{task}.json"
        score, num_sum = evaluator(raw_outputs[task], file_path)

        eval_results = {
                'model_id': [dict_configs.default.model_cfgs.model_id],
                'num_fewshot': [eval_configs.n_shot],
                'chain_of_thought': [eval_configs.cot],
                'num_sum': [num_sum],
                'score': [score]
                }
        logger.print_table(title=f'DrawBench/{task} Benchmark', data=eval_results)
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')
        logger.log('info', f"task: {task}")
        logger.log('info', f"model_id: {eval_results['model_id'][0]},")
        logger.log('info', f"num_fewshot: {eval_results['num_fewshot'][0]},")
        logger.log('info', f"chain_of_thought: {eval_results['chain_of_thought'][0]},")
        logger.log('info', f"num_sum: {eval_results['num_sum'][0]},")
        logger.log('info', f"score: {eval_results['score'][0]},")
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')

if __name__ == '__main__':
    main()
