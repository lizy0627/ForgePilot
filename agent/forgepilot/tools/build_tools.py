from .registry import Tool
from .repository_tools import Arguments
from ..build import BUILD_TOOLS


class BuildTool(Tool):
    arguments_model = Arguments

    def __init__(self, name, pipeline, timeout):
        self.name = name
        self.description = "Run the task's preselected build phase. Runtime approval, state and order are required; accepts no command arguments."
        self.pipeline = pipeline
        self.timeout_seconds = timeout + 30  # Runner owns the actual deadline and tree cleanup.

    async def execute(self, arguments):
        return await self.pipeline.execute(self.name)


def register_build_tools(registry, pipeline):
    config = pipeline.config
    for name, timeout in zip(BUILD_TOOLS, (config.configure_timeout, config.build_timeout, config.test_timeout)):
        registry.register(BuildTool(name, pipeline, timeout))
