from agent import Agent
import json

# Create an agent
agent = Agent()

# Run a task
result = agent.run_task("Search the web and fetch detailed content")

# Print the tools available to the LLM
print("\n=== Tools for LLM ===")
print(json.dumps(agent.get_tools_for_llm(), indent=2))

# Test workflow: search then fetch
print("\n=== Test: Web Search + URL Fetch Workflow ===")

# Step 1: Search
print("\n1. Searching for 'Python programming language'...")
search_result = agent.execute_tool('web_search', {'query': 'Python programming language', 'num_results': 3})
print(f"Found {len(search_result['data']['results'])} results")

# Step 2: Fetch the first result
if search_result['data']['results']:
    first_url = search_result['data']['results'][0]['link']
    print(f"\n2. Fetching content from: {first_url}")
    fetch_result = agent.execute_tool('fetch_url', {'url': first_url})
    
    if 'error' not in fetch_result['data']:
        print(f"Title: {fetch_result['data']['title']}")
        print(f"Content length: {fetch_result['data']['content_length']}")
        print(f"First 300 chars:\n{fetch_result['data']['content'][:300]}...")
    else:
        print(f"Error: {fetch_result['data']['error']}")