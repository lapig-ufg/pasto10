from semente.context import Context
from semente.configs.prompts import get_agent_config


_agent_config = get_agent_config("single_agent")


_AGENT_TOOLS = []


def get_tools(context: Context):
    return _AGENT_TOOLS


def get_instructions(context: Context):
    return _agent_config.get("default")