import json
import os
import urllib.parse
import urllib.request


def get_web_search_tool():
    return {
        'name': 'web_search',
        'description': 'Searches the web using Google via SerpAPI and returns top results',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string'},
                'num_results': {'type': 'number', 'description': 'Number of results to return (default 5)'}
            },
            'required': ['query']
        },
        'execute': _web_search
    }


def _web_search(inputs):
    query = inputs['query']
    num_results = inputs.get('num_results', 5)
    api_key = os.getenv('SERPAPI_KEY')

    if not api_key:
        return {'error': 'SERPAPI_KEY environment variable not set'}

    params = {
        'q': query,
        'api_key': api_key,
        'num': int(num_results)
    }
    query_string = '&'.join(
        [f'{key}={urllib.parse.quote(str(value))}' for key, value in params.items()]
    )
    url = f"https://serpapi.com/search?{query_string}"

    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))

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
    except Exception as error:
        return {'error': str(error), 'query': query}
