"""MCP Agent using Strands SDK patterns."""

import logging
from typing import Dict, List, Any, Optional
from datetime import datetime
from strands import Agent, tool
import strands_tools.file_read as file_read
import strands_tools.file_write as file_write_module
import strands_tools.shell as shell
from strands.tools.mcp import MCPClient
from mcp import stdio_client, StdioServerParameters
from ..config import config
from ..utils.logging import log_title
from ..utils.langfuse_config import langfuse_config
from ..utils.model_providers import get_reasoning_model

logger = logging.getLogger(__name__)

@tool
def file_write(content: str, path: str = None, filename: str = None) -> str:
    """
    Write content to a file. Either path or filename must be provided.
    
    Args:
        content: Content to write to the file
        path: Full path to the file (including filename)
        filename: Name of the file (will be saved in the output directory)
        
    Returns:
        Result of the file write operation
    """
    if path is None and filename is None:
        return "Error: Either path or filename must be provided"
    
    if path is None and filename is not None:
        # Use the output directory from config
        output_dir = getattr(config, "OUTPUT_DIR", "output")
        path = f"{output_dir}/{filename}"
    
    try:
        # Create a ToolUse object for file_write
        tool_use = {
            "toolUseId": "file_write_" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "input": {
                "path": path,
                "content": content
            }
        }
        
        # Call the file_write function from the module
        result = file_write_module.file_write(tool_use)
        return str(result)
    except Exception as e:
        logger.error(f"Error writing to file: {e}")
        return f"Error writing to file: {str(e)}"

@tool
def execute_with_mcp_tools(task_description: str, context: str = "") -> str:
    """
    Execute tasks using available MCP tools.
    
    Args:
        task_description: Description of the task to execute
        context: Additional context for the task
        
    Returns:
        Result of the task execution
    """
    # Create Langfuse span for MCP tool execution
    mcp_span = langfuse_config.create_span(
        trace=None,
        name="mcp-tool-execution",
        input_data={
            "task_description": task_description,
            "context_length": len(context)
        }
    )
    
    try:
        # For now, use built-in tools since MCP server setup requires external process
        # In production, this would connect to actual MCP servers
        
        result = ""
        if "file" in task_description.lower() and "create" in task_description.lower():
            # Handle file creation tasks
            result = f"Task: {task_description}\nContext: {context}\n\nI can help create files using the file_write tool. Please specify the filename and content."
        
        elif "summary" in task_description.lower() or "summarize" in task_description.lower():
            # Handle summarization tasks
            result = f"Based on the context provided:\n{context}\n\nI can create a summary. Please specify what aspects you'd like me to focus on."
        
        else:
            result = f"Task received: {task_description}\nContext length: {len(context)} characters\n\nI'm ready to help with various tasks using available tools."
        
        # Update Langfuse span with results
        if mcp_span and langfuse_config.is_enabled:
            mcp_span.end(output={
                "task_type": "file_creation" if "file" in task_description.lower() else "general",
                "result_length": len(result),
                "success": True
            })
        
        return result
            
    except Exception as e:
        logger.error(f"Error executing MCP task: {e}")
        error_result = f"Error executing task: {str(e)}"
        
        # Update Langfuse span with error
        if mcp_span and langfuse_config.is_enabled:
            mcp_span.end(output={
                "error": str(e),
                "success": False
            })
        
        return error_result

# Create the MCP agent with available tools
mcp_agent = Agent(
    model=get_reasoning_model(),
    tools=[execute_with_mcp_tools, file_write],
    system_prompt="""
You are ToolMaster, a specialized agent for executing tasks using various tools including MCP (Model Context Protocol) tools. Your capabilities include:

1. **Task Execution**: Process user requests and execute them using available tools
2. **File Operations**: Create, read, and modify files as needed
3. **Shell Commands**: Execute system commands when appropriate
4. **Context Integration**: Use provided context to inform your actions

**Available Tools:**
- execute_with_mcp_tools: Execute tasks using MCP tool capabilities
- file_read: Read content from files
- file_write: Write content to files (requires path or filename)
- shell: Execute shell commands

**Instructions:**
- Analyze the user's request and determine the best tools to use
- Use the provided context to inform your responses
- Create files, summaries, or other outputs as requested
- When using file_write, always provide either path or filename parameter
- Handle errors gracefully and provide helpful feedback
- Be thorough and accurate in your task execution

Your goal is to complete user tasks effectively using the available tools and context.
"""
)

def mcp_agent_with_langfuse(query: str) -> str:
    """
    Wrapper for MCP agent with Langfuse tracing.
    
    Args:
        query: User query to process
        
    Returns:
        Agent response as string
    """
    # Create Langfuse trace for the MCP operation
    trace = langfuse_config.create_trace(
        name="mcp-agent-query",
        input_data={"query": query},
        metadata={
            "agent_type": "mcp",
            "model": str(get_reasoning_model()),
            "timestamp": datetime.now().isoformat()
        }
    )
    
    try:
        start_time = datetime.now()
        
        # Call the Strands agent
        response = mcp_agent(query)
        
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()
        
        # Update trace with successful completion
        if trace and langfuse_config.is_enabled:
            trace.update(
                output={
                    "response": str(response),
                    "success": True,
                    "duration_seconds": duration,
                    "response_length": len(str(response))
                }
            )
        
        logger.info(f"MCP agent completed query in {duration:.2f} seconds")
        return str(response)
        
    except Exception as e:
        logger.error(f"Error in MCP agent: {e}")
        
        # Update trace with error
        if trace and langfuse_config.is_enabled:
            trace.update(
                output={
                    "error": str(e),
                    "success": False,
                    "error_type": type(e).__name__
                }
            )
        
        return f"I apologize, but I encountered an error while processing your request: {str(e)}"
    
    finally:
        # Flush Langfuse traces
        if langfuse_config.is_enabled:
            langfuse_config.flush()

# Export both the original agent and the wrapped version
__all__ = ["mcp_agent", "mcp_agent_with_langfuse"]
