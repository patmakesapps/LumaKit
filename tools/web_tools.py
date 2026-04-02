import urllib.request
import urllib.parse
import json
import os

def get_web_search_tool():
    return {
        'name': 'web_search',
        'description': 'Searches the web using Google via SerpAPI and returns top results',
        'inputSchema': {
            'properties': {
                'query': {'type': 'string'},
                'num_results': {'type': 'number', 'description': 'Number of results to return (default 5)'}
            },
            'required': ['query']
        },
        'execute': _web_search_execute
    }

def _web_search_execute(inputs):
    query = inputs['query']
    num_results = inputs.get('num_results', 5)
    api_key = os.getenv('SERPAPI_KEY')
    
    if not api_key:
        return {'error': 'SERPAPI_KEY environment variable not set'}
    
    # SerpAPI endpoint
    params = {
        'q': query,
        'api_key': api_key,
        'num': int(num_results)
    }
    
    url = f"https://serpapi.com/search?{'&'.join([f'{k}={urllib.parse.quote(str(v))}' for k, v in params.items()])}"
    
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
        
        # Extract organic results
        results = []
        if 'organic_results' in data:
            for result in data['organic_results'][:num_results]:
                results.append({
                    'title': result.get('title', ''),
                    'link': result.get('link', ''),
                    'snippet': result.get('snippet', '')
                })
        
        return {
            'query': query,
            'results': results
        }
    except Exception as e:
        return {'error': str(e), 'query': query}


 
    
