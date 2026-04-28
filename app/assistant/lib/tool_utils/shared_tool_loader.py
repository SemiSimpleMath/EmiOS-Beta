def create_tool_loader(tool_class):
    def get_tool_class():
        return tool_class
    return get_tool_class
