"""插件的 LLM 工具（Function Calling）。

统一使用 ``FunctionTool`` 子类而不是 ``@filter.llm_tool`` 装饰器：
- 装饰器版本的参数 schema 只能从 docstring 解析，容易写错且丢失类型；
- 类版本可以直接给出完整 JSON Schema，供模型稳定产出结构化参数。

工具本身保持极薄：只负责取上下文、调用插件方法、返回字符串。
所有业务逻辑与错误处理都在 ``main.py`` 的插件方法里，方便指令与工具复用。
"""

from .todo_tools import TOOL_CLASSES

__all__ = ["TOOL_CLASSES"]
