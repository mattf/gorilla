import re, ast, builtins, ast, json
from bfcl.types import ModelStyle, TestLanguage, TestCategory
from bfcl.model_handler.parser.java_parser import parse_java_function_call
from bfcl.model_handler.parser.javascript_parser import parse_javascript_function_call
from bfcl.model_handler.constants import GORILLA_TO_OPENAPI


def _cast_to_openai_type(properties, mapping):
    for key, value in properties.items():
        if "type" not in value:
            properties[key]["type"] = "string"
        else:
            var_type = value["type"]
            if mapping == GORILLA_TO_OPENAPI and var_type == "float":
                properties[key]["format"] = "float"
                properties[key]["description"] += " This is a float type value."
            if var_type in mapping:
                properties[key]["type"] = mapping[var_type]
            else:
                properties[key]["type"] = "string"

        # Currently support:
        # - list of any
        # - list of list of any
        # - list of dict
        # - list of list of dict
        # - dict of any

        if properties[key]["type"] == "array" or properties[key]["type"] == "object":
            if "properties" in properties[key]:
                properties[key]["properties"] = _cast_to_openai_type(
                    properties[key]["properties"], mapping
                )
            elif "items" in properties[key]:
                properties[key]["items"]["type"] = mapping[
                    properties[key]["items"]["type"]
                ]
                if (
                    properties[key]["items"]["type"] == "array"
                    and "items" in properties[key]["items"]
                ):
                    properties[key]["items"]["items"]["type"] = mapping[
                        properties[key]["items"]["items"]["type"]
                    ]
                elif (
                    properties[key]["items"]["type"] == "object"
                    and "properties" in properties[key]["items"]
                ):
                    properties[key]["items"]["properties"] = _cast_to_openai_type(
                        properties[key]["items"]["properties"], mapping
                    )
    return properties


def convert_to_tool(
    functions, mapping, model_style
):
    oai_tool = []
    for item in functions:
        if "." in item["name"] and (
            model_style == ModelStyle.OPENAI
            or model_style == ModelStyle.MISTRAL
            or model_style == ModelStyle.GOOGLE
            or model_style == ModelStyle.OSS_MODEL
            or model_style == ModelStyle.ANTHROPIC_FC
            or model_style == ModelStyle.COHERE
        ):
            # OAI does not support "." in the function name so we replace it with "_". ^[a-zA-Z0-9_-]{1,64}$ is the regex for the name.
            item["name"] = re.sub(r"\.", "_", item["name"])
            
        item["parameters"]["type"] = "object"
        item["parameters"]["properties"] = _cast_to_openai_type(
            item["parameters"]["properties"], mapping
        )

        if model_style == ModelStyle.ANTHROPIC_FC:
            item["input_schema"] = item["parameters"]
            del item["parameters"]
        if model_style == ModelStyle.GOOGLE:
            # Remove fields that are not supported by Gemini today.
            for params in item["parameters"]["properties"].values():
                if "default" in params:
                    params["description"] += "The Default is:" + str(params["default"])
                    del params["default"]
                if "optional" in params:
                    del params["optional"]
                if "maximum" in params:
                    del params["maximum"]
                if "additionalProperties" in params:
                    params["description"] += "The additional properties:" +str(params["additionalProperties"])
                    del params["additionalProperties"]
        if model_style == ModelStyle.COHERE:
            for params in item["parameters"]["properties"].values():
                if "description" not in params:
                    params["description"] = ""
                if "default" in params:
                    params["description"] += " The default value is: " + str(params["default"])
                    del params["default"]
                if "additionalProperties" in params:
                    params["description"] += " Additional properties: " + str(params["additionalProperties"])
                    del params["additionalProperties"]
                if "items" in params:
                    params["description"] += " List Items type: " + str(params["items"])
                    del params["items"]
                if "properties" in params:
                    params["description"] += " Dictionary properties: " + str(params["properties"])
                    del params["properties"]
        if model_style in [
            ModelStyle.ANTHROPIC_PROMPT,
            ModelStyle.GOOGLE,
            ModelStyle.OSS_MODEL,
        ]:
            oai_tool.append(item)
        elif model_style == ModelStyle.COHERE:
            parameter = item["parameters"]["properties"]
            if "required" in item["parameters"]:
                required = item["parameters"]["required"]
            else:
                required = []
            parameter_definitions = {}
            for key, value in parameter.items():
                value["required"] = key in required
                parameter_definitions[key] = value
            oai_tool.append(
                {
                    "name": item["name"],
                    "description": item["description"],
                    "parameter_definitions": parameter_definitions,
                }
            )
        elif model_style in [
            ModelStyle.OPENAI,
            ModelStyle.MISTRAL,
            ModelStyle.FIREWORK_AI,
        ]:
            oai_tool.append({"type": "function", "function": item})
    return oai_tool


def convert_to_function_call(function_call_list):
    if type(function_call_list) == dict:
        function_call_list = [function_call_list]
    execution_list = []
    for function_call in function_call_list:
        for key, value in function_call.items():
            execution_list.append(
                f"{key}({','.join([f'{k}={repr(v)}' for k,v in json.loads(value).items()])})"
            )
    return execution_list


def convert_value(value, type_str):
    """Convert a string value into its appropriate Python data type based on the provided type string.

    Arg:
        value: the value to convert
        type_str: the type to convert the value to

    Returns:
        The value converted into the requested type or the original value
        if the conversion failed.
    """

    if type_str in ("list", "dict"):
        try:
            return ast.literal_eval(value)
        except:
            return value

    type_class = getattr(builtins, type_str)
    try:
        return type_class(value)
    except ValueError:
        return value


def ast_parse(input_str, language=TestLanguage.PYTHON):
    if language == TestLanguage.PYTHON:
        parsed = ast.parse(input_str, mode="eval")
        extracted = []
        for elem in parsed.body.elts:
            assert isinstance(elem, ast.Call)
            extracted.append(resolve_ast_by_type(elem))
        return extracted
    elif language == TestLanguage.JAVA:
        return parse_java_function_call(
            input_str[1:-1]
        )  # Remove the [ and ] from the string
    elif language == TestLanguage.JAVASCRIPT:
        return parse_javascript_function_call(input_str[1:-1])
    else:
        raise NotImplementedError(f"Unsupported language: {language}")


def resolve_ast_call(elem):
    # Handle nested attributes for deeply nested module paths
    func_parts = []
    func_part = elem.func
    while isinstance(func_part, ast.Attribute):
        func_parts.append(func_part.attr)
        func_part = func_part.value
    if isinstance(func_part, ast.Name):
        func_parts.append(func_part.id)
    func_name = ".".join(reversed(func_parts))
    args_dict = {}
    for arg in elem.keywords:
        output = resolve_ast_by_type(arg.value)
        args_dict[arg.arg] = output
    return {func_name: args_dict}


def resolve_ast_by_type(value):
    if isinstance(value, ast.Constant):
        if value.value is Ellipsis:
            output = "..."
        else:
            output = value.value
    elif isinstance(value, ast.UnaryOp):
        output = -value.operand.value
    elif isinstance(value, ast.List):
        output = [resolve_ast_by_type(v) for v in value.elts]
    elif isinstance(value, ast.Dict):
        output = {
            resolve_ast_by_type(k): resolve_ast_by_type(v)
            for k, v in zip(value.keys, value.values)
        }
    elif isinstance(
        value, ast.NameConstant
    ):  # Added this condition to handle boolean values
        output = value.value
    elif isinstance(
        value, ast.BinOp
    ):  # Added this condition to handle function calls as arguments
        output = eval(ast.unparse(value))
    elif isinstance(value, ast.Name):
        output = value.id
    elif isinstance(value, ast.Call):
        if len(value.keywords)==0:
            output = ast.unparse(value)
        else:
            output = resolve_ast_call(value)
    elif isinstance(value, ast.Tuple):
        output = tuple(resolve_ast_by_type(v) for v in value.elts)
    elif isinstance(value, ast.Lambda):
        output = eval(ast.unparse(value.body[0].value))
    elif isinstance(value, ast.Ellipsis):
        output = "..."
    elif isinstance(value, ast.Subscript):
        try:
            output = ast.unparse(value.body[0].value)
        except:
            output = ast.unparse(value.value) + "[" + ast.unparse(value.slice) + "]"
    else:
        raise Exception(f"Unsupported AST type: {type(value)}")
    return output


def augment_prompt_by_languge(prompt, test_category: TestCategory):
    if test_category == TestCategory.JAVA:
        prompt = prompt + "\n Note that the provided function is in Java 8 SDK syntax."
    elif test_category == TestCategory.JAVASCRIPT:
        prompt = prompt + "\n Note that the provided function is in JavaScript syntax."
    else:
        prompt = prompt + "\n Note that the provided function is in Python 3 syntax."
    return prompt


def language_specific_pre_processing(function, test_category: TestCategory):
    if type(function) is dict:
        function = [function]
    if len(function) == 0:
       return function
    for item in function:
        properties = item["parameters"]["properties"]
        if test_category == TestCategory.JAVA:
            for key, value in properties.items():
                if value["type"] == "any":
                    properties[key]["description"] += (
                        " This parameter can be of any type of Java object in string representation."
                    )
                else:
                    value["description"] += (
                        f" This is Java {value['type']} type parameter in string representation."
                    )
                if value["type"] == "ArrayList" or value["type"] == "Array":
                    value["description"] += (
                        f" The list elements are of type {value['items']['type']}; they are not in string representation."
                    )
                    del value["items"]
                    
                value["type"] = "string"
                
        elif test_category == TestCategory.JAVASCRIPT:
            for key, value in properties.items():
                if value["type"] == "any":
                    properties[key]["description"] += (
                        " This parameter can be of any type of JavaScript object in string representation."
                    )
                else:
                    value["description"] += (
                        f" This is JavaScript {value['type']} type parameter in string representation."
                    )
                if value["type"] == "array":
                    value["description"] += (
                        f" The list elements are of type {value['items']['type']}; they are not in string representation."
                    )
                    del value["items"]
                
                if value["type"] == "dict":
                    if "properties" in value:    # not every dict has properties
                        value["description"] += (
                            f" The dictionary entries have the following schema; they are not in string representation. {json.dumps(value['properties'])}"
                        )
                        del value["properties"]

                value["type"] = "string"
                
    return function


def construct_tool_use_system_prompt(tools):
    tool_use_system_prompt = (
        "In this environment you have access to a set of tools you can use to answer the user's question.\n"
        "\n"
        "You may call them like this:\n"
        "<function_calls>\n"
        "<invoke>\n"
        "<tool_name>$TOOL_NAME</tool_name>\n"
        "<parameters>\n"
        "<$PARAMETER_NAME>$PARAMETER_VALUE</$PARAMETER_NAME>\n"
        "...\n"
        "</parameters>\n"
        "</invoke>\n"
        "</function_calls>\n"
        "\n"
        "Here are the tools available:\n"
        "<tools>\n"
        + "\n".join(
            [
                construct_format_tool_for_claude_prompt(
                    tool["name"], tool["description"], tool["parameters"]["properties"]
                )
                for tool in tools
            ]
        )
        + "\n</tools>"
    )

    return tool_use_system_prompt


def construct_format_tool_for_claude_prompt(name, description, parameters):
    constructed_prompt = (
        "<tool_description>\n"
        f"<tool_name>{name}</tool_name>\n"
        "<description>\n"
        f"{description}\n"
        "</description>\n"
        "<parameters>\n"
        f"{construct_format_parameters_prompt(parameters)}\n"
        "</parameters>\n"
        "</tool_description>"
    )

    return constructed_prompt


def construct_format_parameters_prompt(parameters):
    constructed_prompt = ""
    for parameter_name, parameter in parameters.items():
        if parameter_name == "required":
            continue
        if "description" in parameter:
            description_string = parameter["description"]
        else:
            description_string = ""
        if "default" in parameter:
            description_string += f"\nDefault value: {parameter['default']}"
        elif "items" in parameter:
            description_string += f"\n List element type: {str(parameter['items'])}"
        elif "properties" in parameter:
            description_string += (
                f"\n Dictionaries properties: {str(parameter['properties'])}"
            )
        if "description" in parameter:
            constructed_prompt += f"<parameter>\n<name>{parameter_name}</name>\n<type>{parameter['type']}</type>\n<description>{description_string}</description>\n</parameter>\n"
        else:
            constructed_prompt += f"<parameter>\n<name>{parameter_name}</name>\n<type>{parameter['type']}</type>\n</parameter>\n"
    constructed_prompt = constructed_prompt[:-1]
    return constructed_prompt


def _function_calls_valid_format_and_invoke_extraction(last_completion):
    """Check if the function call follows a valid format and extract the attempted function calls if so. Does not check if the tools actually exist or if they are called with the requisite params."""

    # Check if there are any of the relevant XML tags present that would indicate an attempted function call.
    function_call_tags = re.findall(
        r"<function_calls>|</function_calls>|<invoke>|</invoke>|<tool_name>|</tool_name>|<parameters>|</parameters>",
        last_completion,
        re.DOTALL,
    )
    if not function_call_tags:
        return {"status": True, "invokes": []}

    # Extract content between <function_calls> tags. If there are multiple we will only parse the first and ignore the rest, regardless of their correctness.
    match = re.search(
        r"<function_calls>(.*)</function_calls>", last_completion, re.DOTALL
    )
    if not match:
        return {
            "status": False,
            "reason": "No valid <function_calls></function_calls> tags present in your query.",
        }

    func_calls = match.group(1)

    prefix_match = re.search(r"^(.*?)<function_calls>", last_completion, re.DOTALL)
    if prefix_match:
        func_call_prefix_content = prefix_match.group(1)

    # Check for invoke tags
    invoke_regex = r"<invoke>.*?</invoke>"
    if not re.search(invoke_regex, func_calls, re.DOTALL):
        return {
            "status": False,
            "reason": "Missing <invoke></invoke> tags inside of <function_calls></function_calls> tags.",
        }

    # Check each invoke contains tool name and parameters
    invoke_strings = re.findall(invoke_regex, func_calls, re.DOTALL)
    invokes = []
    for invoke_string in invoke_strings:
        tool_name = re.findall(r"<tool_name>.*?</tool_name>", invoke_string, re.DOTALL)
        if not tool_name:
            return {
                "status": False,
                "reason": "Missing <tool_name></tool_name> tags inside of <invoke></invoke> tags.",
            }
        if len(tool_name) > 1:
            return {
                "status": False,
                "reason": "More than one tool_name specified inside single set of <invoke></invoke> tags.",
            }

        parameters = re.findall(
            r"<parameters>.*?</parameters>", invoke_string, re.DOTALL
        )
        if not parameters:
            return {
                "status": False,
                "reason": "Missing <parameters></paraeters> tags inside of <invoke></invoke> tags.",
            }
        if len(parameters) > 1:
            return {
                "status": False,
                "reason": "More than one set of <parameters></parameters> tags specified inside single set of <invoke></invoke> tags.",
            }

        # Check for balanced tags inside parameters
        tags = re.findall(
            r"<.*?>",
            parameters[0].replace("<parameters>", "").replace("</parameters>", ""),
            re.DOTALL,
        )
        if len(tags) % 2 != 0:
            return {
                "status": False,
                "reason": "Imbalanced tags inside <parameters></parameters> tags.",
            }

        # Loop through the tags and check if each even-indexed tag matches the tag in the position after it (with the / of course). If valid store their content for later use.
        parameters_with_values = []
        for i in range(0, len(tags), 2):
            opening_tag = tags[i]
            closing_tag = tags[i + 1]
            closing_tag_without_second_char = closing_tag[:1] + closing_tag[2:]
            if closing_tag[1] != "/" or opening_tag != closing_tag_without_second_char:
                return {
                    "status": False,
                    "reason": "Non-matching opening and closing tags inside <parameters></parameters> tags.",
                }

            parameters_with_values.append(
                (
                    opening_tag[1:-1],
                    re.search(
                        rf"{opening_tag}(.*?){closing_tag}", parameters[0], re.DOTALL
                    ).group(1),
                )
            )

        # Parse out the full function call
        invokes.append(
            {
                "tool_name": tool_name[0]
                .replace("<tool_name>", "")
                .replace("</tool_name>", ""),
                "parameters_with_values": parameters_with_values,
            }
        )

    return {
        "status": True,
        "invokes": invokes,
        "prefix_content": func_call_prefix_content,
    }


def _convert_value(value, type_str):
    """Convert a string value into its appropriate Python data type based on the provided type string.

    Arg:
        value: the value to convert
        type_str: the type to convert the value to

    Returns:
        The value converted into the requested type or the original value
        if the conversion failed.
    """

    if type_str in ("list", "dict"):
        try:
            return ast.literal_eval(value)
        except:
            return value
    if type_str == "string":
        type_str = "str"
    type_class = getattr(builtins, type_str)
    try:
        return type_class(value)
    except ValueError:
        return value
