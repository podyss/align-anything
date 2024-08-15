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
from align_anything.utils.template_registry import get_template_class
from align_anything.evaluation.data_type import InferenceInput, InferenceOutput
from align_anything.evaluation.eval_logger import EvalLogger
from tqdm import tqdm
from PIL import Image
import re

class MMMUDataLoader(BaseDataLoader):
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
            with open('../cot_fewshot/MMMU/' + task + '.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        else:
            return None
        
    def build_example_prompt(self, data, with_answer=True):
        choices = ''
        if data['question_type'] == 'multiple-choice':
            choices = 'Please choose the correct answer from the following options:\n' + data["options"]
        answer = f'Answer: ({self.get_answer(data)})' if with_answer else 'Answer: '
        return f"Question_type: {data['question_type']}\n{data['question']}{choices}\n{answer}"

    def build_prompt(self, data):
        assert self.num_shot == 0, "MMMU does not support few-shot learning."
        prompt = ""
        template = get_template_class(self.chat_template)
        question = [template.system_prompt + template.user_prompt.format(input=prompt + self.build_example_prompt(item, False)) + template.assistant_prompt.format(output="") for item in data]

        return question
    
    def preprocess(self, data):
        prompts = self.build_prompt(data[self.split])
        raw_images = []
        for item in data[self.split]:
            images = [item[key] for key in get_image_keys(item)]
            raw_images.append(images)
        return prompts, raw_images
    
    def load_dataset(self) -> DatasetDict:
        processed_inputs = {}
        for task in self.task_names:
            dataset = load_dataset(self.task_dir, task)
            self.few_shot_data = self.set_fewshot_dataset(dataset, task)
            prompts, raw_images = self.preprocess(dataset)
            processed_inputs[task] = []
            for prompt, images, question_id in zip(prompts, raw_images, dataset[self.split]['id']):
                image = combine_images(images, direction='horizontal')
                processed_input = InferenceInput(text=prompt, image_file=image)
                processed_input.question_id = question_id
                processed_inputs[task].append(processed_input)
        return processed_inputs
    
class MMMUGeneratorVLLM(BaseInferencer_vllm):
    def eval(self, data:Dict[str, List[InferenceInput]], eval_configs) -> Dict[str, List[InferenceOutput]]:
        task2details = {}
        for task, input in data.items():
            raw_output = self.generation(input)
            for item in raw_output:
                item.prompt = re.sub(r"<image>", "", item.prompt)
                item.raw_output.prompt = re.sub(r"<image>", "", item.raw_output.prompt)
            task2details[task] = raw_output
        
        return task2details
    
    def _generation(self, inputs: List[InferenceInput]) -> List[InferenceOutput]:
        assert isinstance(inputs, list)
        InferenceOutputs = []
        outputs = self.model.generate([{"prompt": input.text, "multi_modal_data": {"image": input.image_file},} for input in inputs], sampling_params=self.samplingparams)
        InferenceOutputs = [InferenceOutput.from_vllm_output(question_id=input.question_id, vllm_output=output, store_raw=True) for output, input in zip(outputs, inputs)]
        return InferenceOutputs

def get_image_keys(data):
    image_labels = []
    for i in range(1, 8):
        key = f"image_{i}"
        if data[key]:
            image_labels.append(key)
    return image_labels

def combine_images(image_list, direction='horizontal'):
    if direction == 'horizontal':
        widths, heights = zip(*(i.size for i in image_list))
        total_width = sum(widths)
        max_height = max(heights)
        new_image = Image.new('RGB', (total_width, max_height))
        x_offset = 0
        for img in image_list:
            new_image.paste(img, (x_offset, 0))
            x_offset += img.width
    else:
        widths, heights = zip(*(i.size for i in image_list))
        max_width = max(widths)
        total_height = sum(heights)
        new_image = Image.new('RGB', (max_width, total_height))
        y_offset = 0
        for img in image_list:
            new_image.paste(img, (0, y_offset))
            y_offset += img.height

    return new_image

def evaluator(test_dataset, output_data, file_path):
    num_match = 0
    num_sum = 0
    question_id = set()
    for test_item in tqdm(test_dataset, desc="Evaluating"):
        for output_item in output_data:
            if test_item['id'] == output_item.question_id and output_item.question_id not in question_id:
                question_id.add(output_item.question_id)
                num_sum += 1
                correct_answer = get_answer(test_item['answer'], test_item['options'])
                true_or_false = judger(correct_answer, output_item.response[0])
                if true_or_false:
                    num_match += 1
                save_detail(test_item['question'], output_item.prompt, correct_answer, output_item.response[0], true_or_false, file_path)

    return num_match, num_sum

def get_answer(answer, options):
    data_list = options.strip("[]").replace("'", "").split(", ")
    return data_list[ord(answer) - 65]

def judger(correct_answer, response):
    if correct_answer in response:
        return True
    return False

def main():
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    _, unparsed_args = parser.parse_known_args()
    keys = [k[2:] for k in unparsed_args[0::2]]
    values = list(unparsed_args[1::2])
    unparsed_args = dict(zip(keys, values))
    logger = EvalLogger('Evaluation')

    dict_configs, infer_configs = read_eval_cfgs('mmmu', 'vLLM')

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
    data_cfgs = dict_configs.default.data_cfgs
    eval_configs = dict_configs.default.eval_cfgs
    logger.log_dir = eval_configs.output_dir
    dataloader = MMMUDataLoader(dict_configs)
    assert not (dataloader.num_shot > 0 and dataloader.cot), "Few-shot and chain-of-thought cannot be used simultaneously for this benchmark."
    test_data = dataloader.load_dataset()
    eval_module = MMMUGeneratorVLLM(model_config, infer_configs)
    raw_outputs = eval_module.eval(test_data, eval_configs)

    os.makedirs(logger.log_dir, exist_ok=True)
    uuid_path = f"{logger.log_dir}/{eval_configs.uuid}"
    os.makedirs(uuid_path, exist_ok=True)

    for task, _ in raw_outputs.items():
        test_data = load_dataset(data_cfgs.task_dir, task)[data_cfgs.split]
        file_path = f"{uuid_path}/{task}.json"
        num_match, num_sum = evaluator(test_data, raw_outputs[task], file_path)
        
        output_dict = {
            'model_id': [dict_configs.default.model_cfgs.model_id],
            'num_match': [num_match],
            'num_sum': [num_sum],
            'accuracy': [num_match / num_sum]
        }
        logger.print_table(title=f'MMMU/{task} Benchmark', data=output_dict)
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')
        logger.log('info', f"task: {task}")
        logger.log('info', f"model_id: {output_dict['model_id'][0]},")
        logger.log('info', f"num_match: {output_dict['num_match'][0]},")
        logger.log('info', f"num_sum: {output_dict['num_sum'][0]},")
        logger.log('info', f"accuracy: {output_dict['accuracy'][0]},")
        logger.log('info', '+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++')

if __name__ == '__main__':
    main()
