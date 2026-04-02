import urllib.request
import urllib.error

def get_url_fetch_tool():
    return {
        'name': 'fetch_url',
        'description': 'Fetches the full content of a webpage from a given URL',
        'inputSchema': {
            'properties': {
                'url': {'type': 'string'}
            },
            'required': ['url']
        },
        'execute': _url_fetch_execute
    }

def _url_fetch_execute(inputs):
    url = inputs['url']
    
    # Validate URL
    if not url.startswith(('http://', 'https://')):
        return {'error': 'URL must start with http:// or https://'}
    
    try:
        # Set a user agent to avoid being blocked
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        req = urllib.request.Request(url, headers=headers)
        
        with urllib.request.urlopen(req, timeout=10) as response:
            content = response.read().decode('utf-8', errors='ignore')
        
        # Extract title if possible (simple extraction)
        title = ""
        if '<title>' in content:
            start = content.find('<title>') + 7
            end = content.find('</title>')
            title = content[start:end].strip()
        
        # Return first 3000 characters to avoid overwhelming the agent
        return {
            'url': url,
            'title': title,
            'content': content[:3000],
            'content_length': len(content)
        }
    except urllib.error.URLError as e:
        return {'error': f'URL Error: {str(e)}', 'url': url}
    except Exception as e:
        return {'error': str(e), 'url': url}