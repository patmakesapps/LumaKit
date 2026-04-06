import urllib.error
import urllib.request


def get_fetch_url_tool():
    return {
        'name': 'fetch_url',
        'description': 'Fetches the full content of a webpage from a given URL',
        'inputSchema': {
            'properties': {
                'url': {'type': 'string'}
            },
            'required': ['url']
        },
        'execute': _fetch_url
    }


def _fetch_url(inputs):
    url = inputs['url']

    if not url.startswith(('http://', 'https://')):
        return {'error': 'URL must start with http:// or https://'}

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        request = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(request, timeout=10) as response:
            content = response.read().decode('utf-8', errors='ignore')

        title = ""
        if '<title>' in content:
            start = content.find('<title>') + 7
            end = content.find('</title>')
            title = content[start:end].strip()

        return {
            'url': url,
            'title': title,
            'content': content[:3000],
            'content_length': len(content)
        }
    except urllib.error.URLError as error:
        return {'error': f'URL Error: {str(error)}', 'url': url}
    except Exception as error:
        return {'error': str(error), 'url': url}
