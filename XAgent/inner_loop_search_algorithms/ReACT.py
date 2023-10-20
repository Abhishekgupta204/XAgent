import yaml
import json
from copy import deepcopy

from colorama import Fore
from XAgent.config import CONFIG
from XAgent.agent.base_agent import BaseAgent
from XAgent.agent.summarize import summarize_action,summarize_plan
from XAgent.data_structure.node import ToolNode
from XAgent.data_structure.tree import TaskSearchTree
from XAgent.inner_loop_search_algorithms.base_search import BaseSearchMethod
from XAgent.loggers.logs import logger, print_assistant_thoughts
from XAgent.message_history import Message
from XAgent.tool_call_handle import function_handler, toolserver_interface
from XAgent.utils import SearchMethodStatusCode, ToolCallStatusCode
from XAgent.data_structure.plan import Plan
from XAgent.global_vars import vector_db_interface
from XAgent.inner_loop_search_algorithms.workflow import InnerWorkFlow
NOW_SUBTASK_PROMPT = '''

'''


def make_message(now_node: ToolNode, task_handler, max_length, config):
    if config.enable_summary:
        terminal_task_info = summarize_plan(
            task_handler.now_dealing_task.to_json())
    else:
        terminal_task_info = json.dumps(
            task_handler.now_dealing_task.to_json(), indent=2, ensure_ascii=False)

    message_sequence = []

    now_subtask_prompt = f'''Now you will perform the following subtask:\n"""\n{terminal_task_info}\n"""\n'''
    message_sequence.append(Message("user", now_subtask_prompt))
    action_process = now_node.process

    if config.enable_summary:
        action_process = summarize_action(
            action_process, terminal_task_info)
    user_prompt = f"""The following steps have been performed (you have already done the following and the current file contents are shown below):\n
    {action_process}
    """
    message_sequence.append(Message("user", user_prompt))
    return message_sequence

def make_workflow_key_sentence(placeholders: dict, tool_functions_description_list: list, message_sequence: list) -> str:
    key_sentence = ""
    key_sentence += f"Plan Overview: {placeholders['system']['all_plan']}\n"
    avaliable_tools = ", ".join([tool_dict['name'] for tool_dict in tool_functions_description_list])
    key_sentence += f"Avaliable Tools: {avaliable_tools}\n"
    key_sentence += f"Current Subtask: {placeholders['user']['subtask_id']}\n"
    key_sentence += f"Max Tool Calls: {placeholders['user']['max_length']}\n"
    key_sentence += f"Tool Call Step Now: {placeholders['user']['step_num']}\n"
    for message in message_sequence:
        message = message.raw()
        key_sentence += f"{message['content']}\n"
    return key_sentence

class ReACTChainSearch(BaseSearchMethod):
    def __init__(self):
        super().__init__()

        self.tree_list = []

    def run(self, config, agent: BaseAgent, task_handler, function_list, tool_functions_description_list, max_try=1,
            max_answer=1):
        for _attempt_id in range(max_try):
            self.generate_chain(config, agent, task_handler,
                                function_list, tool_functions_description_list, )

        if self.status == SearchMethodStatusCode.HAVE_AT_LEAST_ONE_ANSWER:
            self.status = SearchMethodStatusCode.SUCCESS
        else:
            self.status = SearchMethodStatusCode.FAIL

    async def run_async(self, config, agent: BaseAgent, task_handler, function_list, tool_functions_description_list, task_id, max_try=1, max_answer=1, toolserver_interface=None):
        for _attempt_id in range(max_try):
            await self.generate_chain_async(config, agent, task_handler, function_list, tool_functions_description_list, task_id, toolserver_interface)

        if self.status == SearchMethodStatusCode.HAVE_AT_LEAST_ONE_ANSWER:
            self.status = SearchMethodStatusCode.SUCCESS
        else:
            self.status = SearchMethodStatusCode.FAIL

    def get_finish_node(self):
        return self.finish_node

    def generate_chain(self, config, agent: BaseAgent, task_handler, function_list, tool_functions_description_list, ):
        self.tree_list.append(TaskSearchTree())
        now_attempt_tree = self.tree_list[-1]
        now_node = now_attempt_tree.root
        # now_node.workspace_hash_id = start_workspace_hashid

        while now_node.get_depth() < config.max_subtask_chain_length:
            logger.typewriter_log(
                "-=-=-=-=-=-=-= THOUGHTS, REASONING, PLAN AND CRITICISM WILL NOW BE VERIFIED BY AGENT -=-=-=-=-=-=-=",
                Fore.GREEN,
                "",
            )
            message_sequence = make_message(now_node=now_node,
                                            task_handler=task_handler,
                                            max_length=config.max_subtask_chain_length,
                                            config=config)

            
            function_call = None
            if now_node.get_depth() == config.max_subtask_chain_length - 1:
                function_call = {"name": "subtask_submit"}

            file_archi, _, = toolserver_interface.execute_command_client(
                "FileSystemEnv_print_filesys_struture", {"return_root":True})

            human_prompt = ""
            if config.enable_ask_human_for_help:
                human_prompt = "- Use 'ask_human_for_help' when you need help, remember to be specific to your requirement to help user to understand your problem."
            else:
                human_prompt = "- Human is not avaliable for help. You are not allowed to ask human for help in any form or channel. Solve the problem by yourself. If information is not enough, try your best to use default value."
            
            
            all_plan = task_handler.plan_agent.latest_plan.to_json()
            if config.enable_summary:
                all_plan = summarize_plan(all_plan)
            else:
                all_plan = json.dumps(all_plan, indent=2, ensure_ascii=False)
            
            if config.enable_self_evolve_inner_loop:
                inner_yaml_data = yaml.safe_load(open(CONFIG.inner_loop_init_file, "r"))
                # TODO: retrieve workflow
                key_sentence = make_workflow_key_sentence(placeholders, tool_functions_description_list, message_sequence)
                workflows = {}
                if inner_yaml_data["use_predefined"]:
                    workflow_pool = inner_yaml_data["predefined_workflow"]
                else:
                    workflow_pool = vector_db_interface.search_similar_sentences(
                        query_sentence=key_sentence, 
                        namespace=inner_yaml_data["namespace"], 
                        top_k=inner_yaml_data["topk"]
                    )
                for workflow_id in workflow_pool:
                    if inner_yaml_data["use_predefined"]:
                        workflow_yml = workflow_pool[workflow_id]
                    else:
                        workflow_yml = json.loads(workflow_id)
                    workflow = InnerWorkFlow(
                        workflow_yml=workflow_yml,
                        config=config, 
                        tool_jsons=tool_functions_description_list, 
                        toolserver_interface=toolserver_interface
                    )
                    workflow_name = workflow_yml["name"]
                    workflows[workflow_name] = workflow
                    
                for workflow_name in workflows:
                    tool_functions_description_list.append(workflows[workflow_name].workflow_json)
                
            placeholders = {
                "system": {
                    "avaliable_tools": json.dumps(tool_functions_description_list, indent=2, ensure_ascii=False),
                    "all_plan": all_plan
                },
                "user": {
                    "workspace_files": str(file_archi)[:1000]+'`...wrapped...`' if len(str(file_archi)) > 1000 else str(file_archi),
                    "subtask_id": task_handler.now_dealing_task.get_subtask_id(to_str=True),
                    "max_length": config.max_subtask_chain_length,
                    "step_num": str(now_node.get_depth()+1),
                    "human_help_prompt": human_prompt,
                }
            }
            
            if config.enable_self_evolve_inner_loop:
                for _ in range(len(workflows)):
                    tool_functions_description_list.pop()
                
            LLM_code, new_message, tokens = agent.parse(
                placeholders=placeholders,
                functions=function_list,
                function_call=function_call,
                additional_messages=message_sequence,
                additional_insert_index=-1
            )
            new_tree_node = agent.message_to_tool_node(new_message)

            # new_tree_node.history = deepcopy(now_node.history)

            # new_tree_node.history.add("user",DEFAULT_TRIGGERING_PROMPT)

            if "content" in new_message.keys():
                content = new_message["content"]
            else:
                content = ""

            # if "function_call" in new_message.keys():
            #     new_tree_node.history.add("assistant", content, "ai_response", dict(new_message["function_call"]))
            # else:
            #     new_tree_node.history.add("assistant", content, "ai_response")
            print_assistant_thoughts(
                new_tree_node.data, False
            )

            if config.enable_self_evolve_inner_loop:
                # If name match, which means tool agent decide to use workflow, execute the workflow, otherwise run normal inner loop
                if new_tree_node.data["command"]["properties"]["name"] == workflow.workflow_json["name"]:
                    workflow_last_node, tool_output, tool_output_status_code, need_for_plan_refine, using_tools = workflow.run(
                        new_tree_node, now_attempt_tree, function_handler)
                    # TODO: choose the summary actions of the workflow or the last node's output as the tool_output
                    # summary_actions = self.summary_actions(workflow_last_node, task_handler.now_dealing_task)
                    # tool_output = summary_actions
                    now_attempt_tree.make_father_relation(now_node, new_tree_node)
                    # TODO: shall we make tool agent see all the workflow running information? 
                    now_node = workflow_last_node
                else:
                    tool_output, tool_output_status_code, need_for_plan_refine, using_tools = function_handler.handle_tool_call(
                        new_tree_node, task_handler)
                    now_attempt_tree.make_father_relation(now_node, new_tree_node)
                    now_node = new_tree_node
            else:
                tool_output, tool_output_status_code, need_for_plan_refine, using_tools = function_handler.handle_tool_call(
                    new_tree_node, task_handler)
                now_attempt_tree.make_father_relation(now_node, new_tree_node)
                now_node = new_tree_node

            self.need_for_plan_refine = need_for_plan_refine

            if tool_output_status_code == ToolCallStatusCode.SUBMIT_AS_SUCCESS:

                self.status = SearchMethodStatusCode.HAVE_AT_LEAST_ONE_ANSWER
                break
            elif tool_output_status_code == ToolCallStatusCode.SUBMIT_AS_FAILED:
                break

        self.finish_node = now_node

    def get_origin_data(self, data):
        assistant_thoughts_reasoning = None
        assistant_thoughts_plan = None
        assistant_thoughts_speak = None
        assistant_thoughts_criticism = None

        assistant_thoughts = data.get("thoughts", {})
        assistant_thoughts = assistant_thoughts.get("properties", {})
        assistant_thoughts_text = assistant_thoughts.get("thought")
        if assistant_thoughts:
            assistant_thoughts_reasoning = assistant_thoughts.get("reasoning")
            assistant_thoughts_plan = assistant_thoughts.get("plan")
            assistant_thoughts_criticism = assistant_thoughts.get("criticism")

        return {"args": {
            "thoughts": assistant_thoughts_text,
            "reasoning": assistant_thoughts_reasoning,
            "plan": assistant_thoughts_plan,
            "criticism": assistant_thoughts_criticism
        }}

    def rewrite_input_func(self, old, new):
        if not isinstance(new, dict):
            pass
        if new is None:
            return old, False
        else:
            args = new.get("args", {})
            assistant_thoughts_reasoning = None
            assistant_thoughts_plan = None
            assistant_thoughts_speak = None
            assistant_thoughts_criticism = None

            assistant_thoughts = old.get("thoughts", {})
            assistant_thoughts = assistant_thoughts.get("properties", {})
            assistant_thoughts_text = assistant_thoughts.get("thought")
            if assistant_thoughts:
                assistant_thoughts_reasoning = assistant_thoughts.get(
                    "reasoning")
                assistant_thoughts_plan = assistant_thoughts.get("plan")
                assistant_thoughts_criticism = assistant_thoughts.get(
                    "criticism")

                if "thoughts" in args.keys() and "thought" in assistant_thoughts.keys():
                    old["thoughts"]["properties"]["thought"] = args.get(
                        "thoughts", assistant_thoughts_text)
                if "reasoning" in args.keys() and "reasoning" in assistant_thoughts.keys():
                    old["thoughts"]["properties"]["reasoning"] = args.get(
                        "reasoning", assistant_thoughts_reasoning)
                if "plan" in args.keys() and "plan" in assistant_thoughts.keys():
                    old["thoughts"]["properties"]["plan"] = args.get(
                        "plan", assistant_thoughts_plan)
                if "criticism" in args.keys() and "criticism" in assistant_thoughts.keys():
                    old["thoughts"]["properties"]["criticism"] = args.get(
                        "criticism", assistant_thoughts_criticism)

            return old, True
            # if "goal" in args.keys() and "goal" in old_keys.keys():
            #     old.data["thoughts"]["properties"]["goal"] = args.get("goal", old.data.thoughts.goal)
            # if "reasoning" in args.keys() and "reasoning" in old_keys.keys():
            #     old.data["thoughts"]["properties"]["reasoning"] = args.get("reasoning", old.data.thoughts.reasoning)
            # if "plan" in args.keys() and "plan" in old_keys.keys():
            #     old.data.plan = args.get("plan", old.data.thoughts.plan)
            # if "criticism" in args.keys() and "criticism" in old_keys.keys():
            #     old.data.criticism = args.get("criticism", old.data.thoughts.criticism)

    async def generate_chain_async(self, config, agent: BaseAgent, task_handler, function_list, tool_functions_description_list, task_id, toolserver_interface):
        self.tree_list.append(TaskSearchTree())
        now_attempt_tree = self.tree_list[-1]
        now_node = now_attempt_tree.root
        # now_node.workspace_hash_id = start_workspace_hashid

        while now_node.get_depth() < config.max_subtask_chain_length:
            logger.typewriter_log(
                "-=-=-=-=-=-=-= THOUGHTS, REASONING, PLAN AND CRITICISM WILL NOW BE VERIFIED BY AGENT -=-=-=-=-=-=-=",
                Fore.GREEN,
                "",
            )
            if now_node.father != None:
                if task_handler.interaction.interrupt:
                    can_modify = self.get_origin_data(now_node.data)
                    receive_data = await task_handler.interaction.auto_receive(can_modify)
                    data, rewrite_flag = self.rewrite_input_func(
                        now_node.data, receive_data)
                    now_node.data = data
                    if rewrite_flag:
                        logger.typewriter_log(
                            "-=-=-=-=-=-=-= USER INPUT -=-=-=-=-=-=-=",
                            Fore.GREEN,
                            "",
                        )
                        print_assistant_thoughts(now_node.data, False)
                        logger.typewriter_log(
                            "-=-=-=-=-=-=-= USER INPUT -=-=-=-=-=-=-=",
                            Fore.GREEN,
                            "",
                        )

            message_sequence = make_message(now_node=now_node,
                                            task_handler=task_handler,
                                            max_length=config.max_subtask_chain_length,
                                            config=config)

            
            function_call = None
            if now_node.get_depth() == config.max_subtask_chain_length - 1:
                function_call = {"name": "subtask_submit"}

            file_archi, _, = toolserver_interface.execute_command_client(
                "FileSystemEnv_print_filesys_struture",{"return_root":True})

            human_prompt = ""
            if config.enable_ask_human_for_help:
                human_prompt = "- Use 'ask_human_for_help' when you need help, remember to be specific to your requirement to help user to understand your problem."

            all_plan = task_handler.plan_agent.latest_plan.to_json()
            if config.enable_summary:
                all_plan = summarize_plan(all_plan)
            else:
                all_plan = json.dumps(all_plan, indent=2, ensure_ascii=False)
            
            if config.enable_self_evolve_inner_loop:
                inner_yaml_data = yaml.safe_load(open(CONFIG.inner_loop_init_file, "r"))
                # TODO: retrieve workflow
                key_sentence = make_workflow_key_sentence(placeholders, tool_functions_description_list, message_sequence)
                workflows = {}
                if inner_yaml_data["use_predefined"]:
                    workflow_pool = inner_yaml_data["predefined_workflow"]
                else:
                    workflow_pool = vector_db_interface.search_similar_sentences(
                        query_sentence=key_sentence, 
                        namespace=inner_yaml_data["namespace"], 
                        top_k=inner_yaml_data["topk"]
                    )
                for workflow_id in workflow_pool:
                    if inner_yaml_data["use_predefined"]:
                        workflow_yml = workflow_pool[workflow_id]
                    else:
                        workflow_yml = json.loads(workflow_id)
                    workflow = InnerWorkFlow(
                        workflow_yml=workflow_yml,
                        config=config, 
                        tool_jsons=tool_functions_description_list, 
                        toolserver_interface=toolserver_interface
                    )
                    workflow_name = workflow_yml["name"]
                    workflows[workflow_name] = workflow
                    
                for workflow_name in workflows:
                    tool_functions_description_list.append(workflows[workflow_name].workflow_json)
            
            placeholders = {
                "system": {
                    "avaliable_tools": json.dumps(tool_functions_description_list, indent=2, ensure_ascii=False),
                    "all_plan": all_plan
                },
                "user": {
                    "workspace_files": str(file_archi)[:1000]+'`...wrapped...`' if len(str(file_archi)) > 1000 else str(file_archi),
                    "subtask_id": task_handler.now_dealing_task.get_subtask_id(to_str=True),
                    "max_length": config.max_subtask_chain_length,
                    "step_num": str(now_node.get_depth()+1),
                    "human_help_prompt": human_prompt,
                }
            }

            LLM_code, new_message, tokens = agent.parse(
                placeholders=placeholders,
                functions=function_list,
                function_call=function_call,
                additional_messages=message_sequence,
                additional_insert_index=-1
            )

            if config.enable_self_evolve_inner_loop:
                for _ in range(len(workflows)):
                    tool_functions_description_list.pop()

            new_tree_node = agent.message_to_tool_node(new_message)

            # new_tree_node.history = deepcopy(now_node.history)

            # new_tree_node.history.add("user",DEFAULT_TRIGGERING_PROMPT)

            if "content" in new_message.keys():
                content = new_message["content"]
            else:
                content = ""

            # if "function_call" in new_message.keys():
            #     new_tree_node.history.add("assistant", content, "ai_response", dict(new_message["function_call"]))
            # else:
            #     new_tree_node.history.add("assistant", content, "ai_response")
            print_data = print_assistant_thoughts(
                new_tree_node.data, False
            )

            if config.enable_self_evolve_inner_loop:
                # If name match, which means tool agent decide to use workflow, execute the workflow, otherwise run normal inner loop
                if new_tree_node.data["command"]["properties"]["name"] == workflow.workflow_json["name"]:
                    workflow_last_node, tool_output, tool_output_status_code, need_for_plan_refine, using_tools = workflow.run(
                        new_tree_node, now_attempt_tree, function_handler)
                    # TODO: choose the summary actions of the workflow or the last node's output as the tool_output
                    # summary_actions = self.summary_actions(workflow_last_node, task_handler.now_dealing_task)
                    # tool_output = summary_actions
                    now_attempt_tree.make_father_relation(now_node, new_tree_node)
                    # TODO: shall we make tool agent see all the workflow running information? 
                    now_node = workflow_last_node
                else:
                    tool_output, tool_output_status_code, need_for_plan_refine, using_tools = function_handler.handle_tool_call(
                        new_tree_node, task_handler)
                    now_attempt_tree.make_father_relation(now_node, new_tree_node)
                    await task_handler.interaction.update_cache(update_data={**print_data, "using_tools": using_tools}, status="inner", current=task_id)
                    now_node = new_tree_node
            else:
                tool_output, tool_output_status_code, need_for_plan_refine, using_tools = function_handler.handle_tool_call(
                    new_tree_node, task_handler)
                now_attempt_tree.make_father_relation(now_node, new_tree_node)
                await task_handler.interaction.update_cache(update_data={**print_data, "using_tools": using_tools}, status="inner", current=task_id)
                now_node = new_tree_node

            self.need_for_plan_refine = need_for_plan_refine

            if tool_output_status_code == ToolCallStatusCode.SUBMIT_AS_SUCCESS:

                self.status = SearchMethodStatusCode.HAVE_AT_LEAST_ONE_ANSWER
                break
            elif tool_output_status_code == ToolCallStatusCode.SUBMIT_AS_FAILED:
                break

        self.finish_node = now_node

    def to_json(self):
        pass
