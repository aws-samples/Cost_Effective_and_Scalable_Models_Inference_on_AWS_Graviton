"""Supervisor Agent using Strands SDK patterns with Tavily Web Search integration and RAG relevance evaluation."""

# Import global async cleanup FIRST
from ..utils.global_async_cleanup import setup_global_async_cleanup

import asyncio
import re
import logging
import json
from typing import Dict, List, Any, Optional
from datetime import datetime
from strands import Agent, tool
from strands_tools import file_read
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp.mcp_client import MCPClient
from langchain_aws import ChatBedrockConverse
from ragas import SingleTurnSample
from ragas.metrics import LLMContextPrecisionWithoutReference
from ragas.llms import LangchainLLMWrapper
from ..config import config
from ..utils.logging import log_title
from ..utils.model_providers import get_reasoning_model
from ..utils.strands_langfuse_integration import create_traced_agent, setup_tracing_environment
from ..utils.async_cleanup import suppress_async_warnings, setup_async_environment
from ..tools.embedding_retriever import EmbeddingRetriever
from .mcp_agent import file_write  # Use the wrapped file_write from mcp_agent

logger = logging.getLogger(__name__)

# Set up tracing environment and async cleanup
setup_tracing_environment()
setup_async_environment()

# Evaluation model configuration for RAGAs
eval_modelId = 'us.anthropic.claude-3-7-sonnet-20250219-v1:0'
thinking_params = {
    "thinking": {
        "type": "disabled"
    }
}
llm_for_evaluation = ChatBedrockConverse(model_id=eval_modelId, additional_model_request_fields=thinking_params)
llm_for_evaluation = LangchainLLMWrapper(llm_for_evaluation)

# Initialize Tavily MCP client
tavily_mcp_client = None

def get_tavily_mcp_client():
    """Get or create Tavily MCP client"""
    global tavily_mcp_client
    if tavily_mcp_client is None:
        try:
            tavily_mcp_client = MCPClient(lambda: streamablehttp_client("http://localhost:8001/mcp"))
            logger.info("Tavily MCP client initialized successfully")
        except Exception as e:
            logger.warning(f"Failed to initialize Tavily MCP client: {e}")
            tavily_mcp_client = None
    return tavily_mcp_client

def calculate_relevance_score(results: List[Dict], query: str) -> float:
    """
    Calculate relevance score with content validation to prevent false positives.
    
    Args:
        results: List of search results with scores
        query: Original search query for semantic validation
        
    Returns:
        float: Validated relevance score (0.0 to 1.0)
    """
    if not results:
        return 0.0
    
    # Extract scores and validate content relevance
    scores = []
    query_lower = query.lower()
    query_keywords = set(query_lower.split())
    
    for result in results:
        # Get the similarity score
        score = None
        if isinstance(result, dict):
            score = result.get('score') or result.get('_score')
            if score is None and 'metadata' in result:
                score = result['metadata'].get('score')
        
        if score is not None:
            # Validate content relevance by checking keyword overlap
            content = result.get('content', '').lower()
            content_keywords = set(content.split())
            
            # Calculate keyword overlap ratio
            overlap = len(query_keywords.intersection(content_keywords))
            overlap_ratio = overlap / len(query_keywords) if query_keywords else 0
            
            # Penalize results with very low keyword overlap
            if overlap_ratio < 0.1:  # Less than 10% keyword overlap
                score = score * 0.2  # Heavily penalize
            elif overlap_ratio < 0.3:  # Less than 30% keyword overlap
                score = score * 0.5  # Moderately penalize
            
            scores.append(float(score))
    
    if not scores:
        return 0.0
    
    # Calculate average and apply additional validation
    avg_score = sum(scores) / len(scores)
    
    # Additional semantic validation for common mismatches
    if any(keyword in query_lower for keyword in ['weather', 'temperature', 'forecast']):
        # For weather queries, check if results contain weather-related terms
        weather_terms = ['weather', 'temperature', 'rain', 'sunny', 'cloudy', 'forecast', 'celsius', 'fahrenheit']
        has_weather_content = False
        
        for result in results:
            content = result.get('content', '').lower()
            if any(term in content for term in weather_terms):
                has_weather_content = True
                break
        
        if not has_weather_content:
            avg_score = avg_score * 0.1  # Heavily penalize non-weather content for weather queries
    
    return min(avg_score, 1.0)

# Create tools for the supervisor agent
def _run_async_evaluation_safe(scorer, sample):
    """
    Helper function to run async evaluation with proper cleanup and error handling.
    
    Args:
        scorer: RAGAs scorer instance
        sample: SingleTurnSample for evaluation
        
    Returns:
        float: Evaluation score
    """
    import asyncio
    import threading
    import queue
    
    def run_evaluation():
        """Run the evaluation in a clean async environment."""
        async def evaluate():
            try:
                # Set a shorter timeout for the evaluation
                score = await asyncio.wait_for(
                    scorer.single_turn_ascore(sample), 
                    timeout=20.0  # 20 second timeout
                )
                return score
            except asyncio.TimeoutError:
                raise TimeoutError("RAGAs evaluation timed out")
            except Exception as e:
                raise Exception(f"RAGAs evaluation failed: {str(e)}")
        
        # Create and run in a new event loop with proper cleanup
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            score = loop.run_until_complete(evaluate())
            result_queue.put(('success', score))
        except Exception as e:
            result_queue.put(('error', str(e)))
        finally:
            # Properly close the loop and clean up
            try:
                # Cancel all remaining tasks
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                
                # Wait for tasks to complete cancellation with timeout
                if pending:
                    try:
                        loop.run_until_complete(
                            asyncio.wait_for(
                                asyncio.gather(*pending, return_exceptions=True),
                                timeout=2.0
                            )
                        )
                    except asyncio.TimeoutError:
                        pass  # Ignore timeout during cleanup
                
                # Close the loop
                loop.close()
            except Exception:
                pass  # Ignore cleanup errors
    
    # Use a queue to communicate between threads
    result_queue = queue.Queue()
    
    # Run in a separate thread to avoid event loop conflicts
    thread = threading.Thread(target=run_evaluation, daemon=True)
    thread.start()
    thread.join(timeout=25)  # Give extra time for cleanup
    
    if thread.is_alive():
        raise TimeoutError("Evaluation thread timed out")
    
    try:
        result_type, result_value = result_queue.get_nowait()
        if result_type == 'error':
            raise Exception(result_value)
        return result_value
    except queue.Empty:
        raise Exception("No result received from evaluation")

@tool
def check_chunks_relevance(results: str, question: str):
    """
    Evaluates the relevance of retrieved chunks to the user question using RAGAs.

    Args:
        results (str): Retrieval output as a string with 'Score:' and 'Content:' patterns.
        question (str): Original user question.

    Returns:
        dict: A binary score ('yes' or 'no') and the numeric relevance score, or an error message.
    """
    # Use the async warning suppression context
    with suppress_async_warnings():
        try:
            if not results or not isinstance(results, str):
                raise ValueError("Invalid input: 'results' must be a non-empty string.")
            if not question or not isinstance(question, str):
                raise ValueError("Invalid input: 'question' must be a non-empty string.")

            # Extract content chunks using regex with improved error handling
            patterns_to_try = [
                r"Score:.*?\nContent:\s*(.*?)(?=\n\nScore:|\Z)",  # Handle double newlines
                r"Score:.*?\nContent:\s*(.*?)(?=Score:|\Z)",      # Original pattern
                r"Score:\s*[\d.]+\s*\nContent:\s*(.*?)(?=\n\nScore:|\Z)",  # More specific score pattern
            ]
            
            docs = []
            pattern_used = None
            
            for i, pattern in enumerate(patterns_to_try):
                try:
                    docs = [chunk.strip() for chunk in re.findall(pattern, results, re.DOTALL) if chunk.strip()]
                    if docs:
                        pattern_used = f"Pattern {i+1}"
                        logger.debug(f"Successfully extracted {len(docs)} chunks using pattern {i+1}")
                        break
                except Exception as e:
                    logger.warning(f"Pattern {i+1} failed: {e}")
                    continue
            
            # If no patterns worked, try a more flexible approach
            if not docs:
                logger.warning("Standard patterns failed, trying flexible extraction...")
                flexible_pattern = r"Content:\s*([^\n]+(?:\n(?!Score:)[^\n]*)*)"
                try:
                    docs = [chunk.strip() for chunk in re.findall(flexible_pattern, results, re.MULTILINE) if chunk.strip()]
                    if docs:
                        pattern_used = "Flexible pattern"
                        logger.info(f"Flexible extraction found {len(docs)} chunks")
                except Exception as e:
                    logger.error(f"Flexible extraction also failed: {e}")

            if not docs:
                # Provide detailed debugging information
                debug_info = {
                    "results_length": len(results),
                    "contains_score": "Score:" in results,
                    "contains_content": "Content:" in results,
                    "results_preview": results[:200] if len(results) > 200 else results
                }
                logger.error(f"No valid content chunks found. Debug info: {debug_info}")
                raise ValueError(f"No valid content chunks found in 'results'. Debug: {debug_info}")

            # Limit the number of chunks to avoid timeout
            if len(docs) > 3:
                docs = docs[:3]  # Take only first 3 chunks
                logger.info(f"Limited evaluation to first 3 chunks out of {len(docs)} total")

            # Generate a proper answer from the context for RAGAs evaluation
            # This is necessary because LLMContextPrecisionWithoutReference evaluates
            # whether the context was useful in arriving at the given answer
            try:
                context_for_answer = "\n\n".join(docs[:2])  # Use first 2 chunks
                answer_prompt = f"""Based on the following context, provide a brief answer to the question.

Question: {question}
Context: {context_for_answer}

Answer:"""
                
                # Use a simple model call to generate the answer
                from langchain_aws import ChatBedrockConverse
                answer_llm = ChatBedrockConverse(model_id='us.anthropic.claude-3-7-sonnet-20250219-v1:0')
                answer_response = answer_llm.invoke(answer_prompt)
                generated_answer = answer_response.content.strip()
                
                logger.debug(f"Generated answer for RAGAs evaluation: {generated_answer[:100]}...")
                
            except Exception as e:
                logger.warning(f"Failed to generate answer for RAGAs evaluation: {e}")
                generated_answer = "Unable to generate answer from context"

            # Prepare evaluation sample with proper answer
            sample = SingleTurnSample(
                user_input=question,
                response=generated_answer,  # Use generated answer instead of placeholder
                retrieved_contexts=docs
            )

            # Evaluate using context precision metric with safe async handling
            scorer = LLMContextPrecisionWithoutReference(llm=llm_for_evaluation)
            
            print("------------------------")
            print("Context evaluation (RAGAs)")
            print("------------------------")
            print(f"Evaluating {len(docs)} chunks...")
            
            # Use the safe evaluation helper
            score = _run_async_evaluation_safe(scorer, sample)

            print(f"chunk_relevance_score: {score}")
            print("------------------------")

            return {
                "chunk_relevance_score": "yes" if score > 0.5 else "no",
                "chunk_relevance_value": score
            }

        except Exception as e:
            logger.error(f"Error in chunk relevance evaluation: {e}")
            # Provide a fallback based on simple heuristics
            try:
                # Simple fallback: check if question keywords appear in results
                question_words = set(question.lower().split())
                results_words = set(results.lower().split())
                overlap = len(question_words.intersection(results_words))
                fallback_score = min(overlap / len(question_words), 1.0) if question_words else 0.0
                
                logger.info(f"Using fallback relevance score: {fallback_score}")
                
                return {
                    "chunk_relevance_score": "yes" if fallback_score > 0.3 else "no",
                    "chunk_relevance_value": fallback_score,
                    "evaluation_method": "fallback_heuristic",
                    "note": f"RAGAs evaluation failed, using keyword overlap heuristic"
                }
            except Exception as fallback_error:
                logger.error(f"Fallback evaluation also failed: {fallback_error}")
                return {
                    "error": f"Both RAGAs and fallback evaluation failed: {str(e)}",
                    "chunk_relevance_score": "unknown",
                    "chunk_relevance_value": None
                }

@tool
def search_knowledge_base(query: str, top_k: int = 3) -> str:  
    """
    Search the knowledge base for relevant information.
    
    Args:
        query (str): The search query - REQUIRED
        top_k (int): Number of top results to return (default: 3)
        
    Returns:
        str: JSON string with search results and relevance metadata
    """
    if not query or not isinstance(query, str):
        return '{"error": "Query parameter is required and must be a non-empty string", "results": [], "relevance_score": 0.0}'
    
    try:
        retriever = EmbeddingRetriever()
        results = retriever.search(query, top_k=top_k)
        
        # Calculate relevance score with content validation
        relevance_score = calculate_relevance_score(results, query)
        
        # Remove duplicate results
        seen_content = set()
        unique_results = []
        for result in results:
            content_hash = hash(result['content'][:100])  # Use first 100 chars as hash
            if content_hash not in seen_content:
                seen_content.add(content_hash)
                unique_results.append(result)
        
        # Format results for RAGAs evaluation (with Score: and Content: patterns)
        formatted_for_evaluation = ""
        for result in unique_results[:top_k]:
            formatted_for_evaluation += f"Score: {result.get('score', result.get('_score', 0.0))}\n"
            formatted_for_evaluation += f"Content: {result['content']}\n\n"
        
        # Format results as compact JSON for response
        formatted_results = []
        for result in unique_results[:top_k]:  # Ensure we don't exceed top_k after deduplication
            # Limit content length to reduce tokens
            content = result['content']
            if len(content) > 200:  
                content = content[:200] + "..."
                
            formatted_results.append({
                "source": result['metadata'].get('source', 'Unknown'),
                "content": content,
                "score": result.get('score', result.get('_score', 0.0))
            })
        
        # Create response with relevance metadata and validation info
        response_data = {
            "results": formatted_results,
            "relevance_score": relevance_score,
            "total_results": len(unique_results),
            "duplicates_removed": len(results) - len(unique_results),
            "query": query,
            "validation_note": "Relevance score includes content validation to prevent false positives",
            "formatted_for_evaluation": formatted_for_evaluation  # Add this for RAGAs evaluation
        }
        
        # Convert to JSON string
        response = json.dumps(response_data, indent=2)
        
        # Log successful search with debug info
        logger.info(f"Knowledge base search completed: {len(unique_results)} unique results (removed {len(results) - len(unique_results)} duplicates), relevance: {relevance_score:.2f}")
        
        # Debug logging for relevance issues
        if relevance_score < 0.3:
            logger.debug(f"Low relevance detected for query '{query}': {relevance_score:.2f}")
            for i, result in enumerate(formatted_results[:2]):  # Log first 2 results for debugging
                logger.debug(f"Result {i+1}: {result['content'][:50]}... (score: {result['score']:.2f})")
        
        return f"<search_results>\n{response}\n</search_results>"
        
    except Exception as e:
        logger.error(f"Error searching knowledge base: {e}")
        error_response = {
            "error": f"Error searching knowledge base: {str(e)}",
            "results": [],
            "relevance_score": 0.0,
            "query": query
        }
        return json.dumps(error_response)

@tool
def check_knowledge_status() -> str:
    """
    Check the status of the knowledge base.
    
    Returns:
        str: JSON string with knowledge base status
    """
    try:
        retriever = EmbeddingRetriever()
        count = retriever.get_document_count()
        
        # Format as compact JSON to reduce token usage
        status_data = {
            "status": "ready" if count > 0 else "empty",
            "document_count": count,
            "last_updated": datetime.now().strftime("%Y-%m-%d")
        }
        response = json.dumps(status_data)
        
        # Log successful status check
        logger.info(f"Knowledge base status checked: {count} documents")
        
        return response
        
    except Exception as e:
        logger.error(f"Error checking knowledge status: {e}")
        error_msg = f'{{"error": "Failed to check knowledge status: {str(e)}", "status": "error"}}'
        return error_msg

# Create the supervisor agent with tracing and enhanced tools including MCP tools
def create_supervisor_agent_with_mcp():
    """Create supervisor agent with MCP tools integrated using proper context manager"""
    
    # Get MCP client
    mcp_client = get_tavily_mcp_client()
    
    if mcp_client:
        # Use the MCP client context manager as per Strands SDK documentation
        with mcp_client:
            # Get the tools from the MCP server
            mcp_tools = mcp_client.list_tools_sync()
            logger.info(f"Loaded {len(mcp_tools)} MCP tools from Tavily server")
            
            # Combine local tools with MCP tools
            all_tools = [
                check_chunks_relevance,
                search_knowledge_base, 
                check_knowledge_status, 
                file_read, 
                file_write
            ] + mcp_tools
            
            # Create agent within the MCP context
            return create_traced_agent(
                Agent,
                model=get_reasoning_model(),
                tools=all_tools,
                system_prompt="""
You are a RAG system with web search capabilities. Answer questions using retrieved info and real-time web data.

WORKFLOW:
1. ALWAYS start with check_knowledge_status() - verify knowledge base first
2. search_knowledge_base(query="terms") - search internal data (returns JSON with relevance_score)
3. Check the relevance_score: if < 0.3 OR for time-sensitive queries (weather, news, "today", "current"): use web_search
4. If relevance_score >= 0.3 and not time-sensitive: use RAG results
5. When writing files, ALWAYS use the output directory - call file_write with filename parameter only
6. Cite sources clearly

TOOLS AVAILABLE:
- check_knowledge_status(): Check KB status - ALWAYS CALL THIS FIRST
- search_knowledge_base(query): Search KB (returns JSON with relevance_score)
- web_search(query, max_results, search_depth, include_answer): MCP tool for web search
- news_search(query, max_results, days_back): MCP tool for news search
- health_check(): MCP tool to check Tavily service status
- file_read(path): Read files
- file_write(content, filename): Write files to output directory (use filename parameter, not path)

DECISION LOGIC:
1. FIRST: Always call check_knowledge_status()
2. For weather/news/current events: use web_search directly
3. For other queries: search knowledge base first, check relevance_score
4. If relevance_score < 0.3: use web_search for better results
5. If relevance_score >= 0.3: use RAG results
6. FINAL: When saving files, use file_write(content, filename) - files go to output directory automatically

FORMAT: Be concise, cite sources, use bullets when helpful

IMPORTANT: 
- ALWAYS start with check_knowledge_status()
- ALWAYS use filename parameter (not path) for file_write to save to output directory
""",
                session_id="supervisor-session",
                user_id="system"
            )
    else:
        # Fallback: create agent without MCP tools
        logger.warning("Creating agent without MCP tools due to client unavailability")
        return create_traced_agent(
            Agent,
            model=get_reasoning_model(),
            tools=[
                check_chunks_relevance,
                search_knowledge_base, 
                check_knowledge_status, 
                file_read, 
                file_write
            ],
            system_prompt="""
You are a RAG system with advanced relevance evaluation. Answer questions using retrieved information from the knowledge base.

ENHANCED WORKFLOW WITH RAG EVALUATION:
1. ALWAYS start with check_knowledge_status() - verify knowledge base first
2. search_knowledge_base(query="terms") - search internal data (returns JSON with formatted_for_evaluation field)
3. EVALUATE RELEVANCE: Use check_chunks_relevance(results=formatted_for_evaluation, question=original_query)
   - This returns {"chunk_relevance_score": "yes"/"no", "chunk_relevance_value": float}
4. DECISION POINT:
   - If chunk_relevance_score is "yes" (score > 0.5): Use RAG results to answer
   - If chunk_relevance_score is "no" (score <= 0.5): Inform user that results may not be relevant
5. When writing files, ALWAYS use the output directory - call file_write with filename parameter only
6. Cite sources clearly and mention evaluation results

TOOLS AVAILABLE:
- check_knowledge_status(): Check KB status - ALWAYS CALL THIS FIRST
- search_knowledge_base(query): Search KB (returns JSON with formatted_for_evaluation field)
- check_chunks_relevance(results, question): Evaluate relevance using RAGAs (use formatted_for_evaluation field)
- file_read(path): Read files
- file_write(content, filename): Write files to output directory (use filename parameter, not path)

ENHANCED DECISION LOGIC:
1. FIRST: Always call check_knowledge_status()
2. SECOND: search_knowledge_base(query) to get results with formatted_for_evaluation
3. THIRD: check_chunks_relevance(results=formatted_for_evaluation, question=original_query)
4. DECISION:
   - If chunk_relevance_score is "yes": Use RAG results confidently
   - If chunk_relevance_score is "no": Mention low relevance and provide best available information
5. FINAL: When saving files, use file_write(content, filename) - files go to output directory automatically

IMPORTANT: 
- ALWAYS start with check_knowledge_status()
- ALWAYS evaluate chunk relevance before providing answers
- ALWAYS use filename parameter (not path) for file_write to save to output directory
- Be transparent about relevance evaluation results

FORMAT: Be concise, cite sources, use bullets when helpful, mention evaluation results
""",
            session_id="supervisor-session",
            user_id="system"
        )

# Create a wrapper class to handle MCP context properly
class SupervisorAgentWrapper:
    """Wrapper to handle MCP client context for supervisor agent"""
    
    def __init__(self):
        self.mcp_client = get_tavily_mcp_client()
        self.agent = None
        self._create_agent()
    
    def _create_agent(self):
        """Create the agent with proper MCP context"""
        if self.mcp_client:
            with self.mcp_client:
                # Get the tools from the MCP server
                mcp_tools = self.mcp_client.list_tools_sync()
                logger.info(f"Loaded {len(mcp_tools)} MCP tools from Tavily server")
                
                # Combine local tools with MCP tools
                all_tools = [
                    check_chunks_relevance,
                    search_knowledge_base, 
                    check_knowledge_status, 
                    file_read, 
                    file_write
                ] + mcp_tools
                
                # Create agent within the MCP context
                self.agent = create_traced_agent(
                    Agent,
                    model=get_reasoning_model(),
                    tools=all_tools,
                    system_prompt="""
You are a RAG system with web search capabilities. Answer questions using retrieved info and real-time web data.

WORKFLOW:
1. check_knowledge_status() - verify knowledge base
2. search_knowledge_base(query="terms") - search internal data
4. If recommendation is "USE_WEB_SEARCH": use web_search or news_search MCP tools
5. If recommendation is "USE_RAG_RESULTS": use the RAG results
6. Cite sources clearly

TOOLS AVAILABLE:
- check_knowledge_status(): Check KB status
- search_knowledge_base(query): Search KB (returns relevance_score)
- web_search(query, max_results, search_depth, include_answer): MCP tool for web search
- news_search(query, max_results, days_back): MCP tool for news search
- health_check(): MCP tool to check Tavily service status
- file_read(path): Read files
- file_write(content, path): Write files

DECISION LOGIC:
1. Always search knowledge base first
3. Follow the recommendation (USE_WEB_SEARCH or USE_RAG_RESULTS)
4. For weather, news, or current events: prefer web search
5. For established knowledge: prefer RAG results

FORMAT: Be concise, cite sources, use bullets when helpful
""",
                    session_id="supervisor-session",
                    user_id="system"
                )
        else:
            # Fallback: create agent without MCP tools
            logger.warning("Creating agent without MCP tools due to client unavailability")
            self.agent = create_traced_agent(
                Agent,
                model=get_reasoning_model(),
                tools=[
                    check_chunks_relevance,
                    search_knowledge_base, 
                    check_knowledge_status, 
                    file_read, 
                    file_write
                ],
                system_prompt="""
You are a RAG system. Answer questions using retrieved information from the knowledge base.

WORKFLOW:
1. ALWAYS start with check_knowledge_status() - verify knowledge base first
2. search_knowledge_base(query="terms") - search internal data
3. Use the retrieved information to answer questions
4. When writing files, ALWAYS use the output directory - call file_write with filename parameter only
5. Cite sources clearly

TOOLS AVAILABLE:
- check_knowledge_status(): Check KB status - ALWAYS CALL THIS FIRST
- search_knowledge_base(query): Search KB (returns relevance_score)
- file_read(path): Read files
- file_write(content, filename): Write files to output directory (use filename parameter, not path)

IMPORTANT: 
- ALWAYS start with check_knowledge_status()
- ALWAYS use filename parameter (not path) for file_write to save to output directory

FORMAT: Be concise, cite sources, use bullets when helpful
""",
                session_id="supervisor-session",
                user_id="system"
            )
    
    def __call__(self, query: str):
        """Call the agent with proper MCP context"""
        if self.mcp_client:
            with self.mcp_client:
                return self.agent(query)
        else:
            return self.agent(query)

# Create the default supervisor agent
supervisor_agent = SupervisorAgentWrapper()

def create_fresh_supervisor_agent():
    """
    Create a fresh supervisor agent instance with no conversation history.
    This ensures each query starts with a clean context window.
    """
    import uuid
    
    # Create a unique session ID for each fresh agent
    fresh_session_id = f"supervisor-{uuid.uuid4().hex[:8]}"
    
    # Return a fresh wrapper instance
    class FreshSupervisorAgentWrapper:
        """Fresh wrapper to handle MCP client context for supervisor agent"""
        
        def __init__(self, session_id):
            self.mcp_client = get_tavily_mcp_client()
            self.agent = None
            self.session_id = session_id
            self._create_agent()
        
        def _create_agent(self):
            """Create the agent with proper MCP context"""
            if self.mcp_client:
                with self.mcp_client:
                    # Get the tools from the MCP server
                    mcp_tools = self.mcp_client.list_tools_sync()
                    logger.info(f"Loaded {len(mcp_tools)} MCP tools from Tavily server for fresh agent")
                    
                    # Combine local tools with MCP tools
                    all_tools = [
                        check_chunks_relevance,
                        search_knowledge_base, 
                        check_knowledge_status, 
                        file_read, 
                        file_write
                    ] + mcp_tools
                    
                    # Create agent within the MCP context
                    self.agent = create_traced_agent(
                        Agent,
                        model=get_reasoning_model(),
                        tools=all_tools,
                        system_prompt="""
You are a RAG system with web search capabilities and advanced relevance evaluation. Answer questions using retrieved info and real-time web data.

ENHANCED WORKFLOW WITH RAG EVALUATION:
1. ALWAYS start with check_knowledge_status() - verify knowledge base first
2. search_knowledge_base(query="terms") - search internal data (returns JSON with formatted_for_evaluation field)
3. EVALUATE RELEVANCE: Use check_chunks_relevance(results=formatted_for_evaluation, question=original_query)
   - This returns {"chunk_relevance_score": "yes"/"no", "chunk_relevance_value": float}
4. DECISION POINT:
   - If chunk_relevance_score is "yes" (score > 0.5): Use RAG results to answer
   - If chunk_relevance_score is "no" (score <= 0.5): Use web_search for better results
   - For time-sensitive queries (weather, news, "today", "current"): Always use web_search
5. When writing files, ALWAYS use the output directory - call file_write with filename parameter only
6. Cite sources clearly and mention which evaluation method was used

TOOLS AVAILABLE:
- check_knowledge_status(): Check KB status - ALWAYS CALL THIS FIRST
- search_knowledge_base(query): Search KB (returns JSON with formatted_for_evaluation field)
- check_chunks_relevance(results, question): Evaluate relevance using RAGAs (use formatted_for_evaluation field)
- web_search(query, max_results, search_depth, include_answer): MCP tool for web search
- news_search(query, max_results, days_back): MCP tool for news search
- health_check(): MCP tool to check Tavily service status
- file_read(path): Read files
- file_write(content, filename): Write files to output directory (use filename parameter, not path)

ENHANCED DECISION LOGIC:
1. FIRST: Always call check_knowledge_status()
2. SECOND: search_knowledge_base(query) to get results with formatted_for_evaluation
3. THIRD: check_chunks_relevance(results=formatted_for_evaluation, question=original_query)
4. DECISION:
   - If chunk_relevance_score is "yes": Use RAG results
   - If chunk_relevance_score is "no": Use web_search
   - For weather/news/current events: Skip evaluation, use web_search directly
5. FINAL: When saving files, use file_write(content, filename) - files go to output directory automatically

FORMAT: Be concise, cite sources, use bullets when helpful, mention evaluation results

IMPORTANT: 
- ALWAYS start with check_knowledge_status()
- ALWAYS evaluate chunk relevance before deciding between RAG and web search
- ALWAYS use filename parameter (not path) for file_write to save to output directory
- Be transparent about which source provided the information and the relevance evaluation results
""",
                        session_id=self.session_id,
                        user_id="system"
                    )
            else:
                # Fallback: create agent without MCP tools
                logger.warning("Creating fresh agent without MCP tools due to client unavailability")
                self.agent = create_traced_agent(
                    Agent,
                    model=get_reasoning_model(),
                    tools=[
                        check_chunks_relevance,
                        search_knowledge_base, 
                        check_knowledge_status, 
                        file_read, 
                        file_write
                    ],
                    system_prompt="""
You are a RAG system. Answer questions using retrieved information from the knowledge base.

WORKFLOW:
1. ALWAYS start with check_knowledge_status() - verify knowledge base first
2. search_knowledge_base(query="terms") - search internal data
3. Use the retrieved information to answer questions
4. When writing files, ALWAYS use the output directory - call file_write with filename parameter only
5. Cite sources clearly

TOOLS AVAILABLE:
- check_knowledge_status(): Check KB status - ALWAYS CALL THIS FIRST
- search_knowledge_base(query): Search KB (returns relevance_score)
- file_read(path): Read files
- file_write(content, filename): Write files to output directory (use filename parameter, not path)

IMPORTANT: 
- ALWAYS start with check_knowledge_status()
- ALWAYS use filename parameter (not path) for file_write to save to output directory

FORMAT: Be concise, cite sources, use bullets when helpful
""",
                    session_id=self.session_id,
                    user_id="system"
                )
        
        def __call__(self, query: str):
            """Call the agent with proper MCP context"""
            if self.mcp_client:
                with self.mcp_client:
                    return self.agent(query)
            else:
                return self.agent(query)
    
    return FreshSupervisorAgentWrapper(fresh_session_id)

# The supervisor_agent now has built-in tracing via Strands SDK and proper MCP integration
# Export the agent and the fresh agent creator
__all__ = ["supervisor_agent", "create_fresh_supervisor_agent"]
