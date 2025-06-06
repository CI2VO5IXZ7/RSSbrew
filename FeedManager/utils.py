import re
from bs4 import BeautifulSoup
import logging
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
import os
from openai import OpenAI
import tiktoken

logger = logging.getLogger('feed_logger')
OPENAI_PROXY = os.environ.get('OPENAI_PROXY')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
OPENAI_BASE_URL = os.environ.get('OPENAI_BASE_URL') or 'https://api.openai.com/v1'

def remove_control_characters(s):
    control_chars = ''.join(map(chr, range(0, 32))) + chr(127)
    control_char_re = re.compile('[%s]' % re.escape(control_chars))
    return control_char_re.sub('', s)

def clean_url(url):
    parsed_url = urlparse(url)
    
    # 解析查询字符串
    query_params = parse_qs(parsed_url.query)
    
    # 移除 'hl' 参数（如果存在）
    query_params.pop('hl', None)
    
    # 重新构建查询字符串
    new_query = urlencode(query_params, doseq=True)
    
    # 重建 URL，只将域名部分转为小写，保持路径的原始大小写
    clean_url = urlunparse((
        parsed_url.scheme,
        parsed_url.netloc.lower(),  # 只对域名部分转小写
        parsed_url.path.rstrip('/'),  # 保持路径大小写
        '',
        new_query,
        ''
    ))
    
    # 如果清理后的 URL 以 '?' 结尾，则移除它
    if clean_url.endswith('?'):
        clean_url = clean_url[:-1]
    
    return clean_url

def clean_html(html_content):
    """
    This function is used to clean the HTML content.
    It will remove all the <script>, <style>, <img>, <a>, <video>, <audio>, <iframe>, <input> tags.
    Returns:
        Cleaned text for summarization
    """
    soup = BeautifulSoup(html_content, "html.parser")

    for script in soup.find_all("script"):
        script.decompose()

    for style in soup.find_all("style"):
        style.decompose()

    for img in soup.find_all("img"):
        img.decompose()

    for a in soup.find_all("a"):
        a.decompose()

    for video in soup.find_all("video"):
        video.decompose()

    for audio in soup.find_all("audio"):
        audio.decompose()
    
    for iframe in soup.find_all("iframe"):
        iframe.decompose()
    
    for input in soup.find_all("input"):
        input.decompose()

    return soup.get_text()

def clean_txt_and_truncate(query, model, clean_bool=True):
    cleaned_article = query
    if clean_bool:
        cleaned_article = clean_html(query)
    try:
        encoding = tiktoken.encoding_for_model(model)
    except:
        encoding = tiktoken.encoding_for_model('gpt-4o')
    token_length = len(encoding.encode(cleaned_article))

    max_length_of_models = {
        'gpt-3.5-turbo': 16200,
        'gpt-4.1': 1047376,
        'gpt-4.1-mini': 1047376,
        'gpt-4.1-nano': 1047376,
        'default': 127800  # Default for all other models
    }

    max_length = max_length_of_models.get(model, max_length_of_models['default'])

    # Truncate the text if it exceeds the model's token limit
    if token_length > max_length:
        truncated_article = encoding.decode(encoding.encode(cleaned_article)[:max_length])
        return truncated_article
    else:
        return cleaned_article

def generate_untitled(entry):
    try: return entry.title
    except: 
        try: return entry.article[:50]
        except: return entry.link

def passes_filters(entry, processed_feed, filter_type):
    groups = processed_feed.filter_groups.filter(usage=filter_type)
    if not groups:
        return True
    group_results = []
    for group in groups:
        filters = group.filters.all()
        results = [match_content(entry, filter, processed_feed.case_sensitive) for filter in filters]
        logger.debug(f'  Results for group {group.usage}: {results} for {entry.title} {entry.link}')
        if group.relational_operator == 'all':
            group_results.append(all(results))
        elif group.relational_operator == 'any':
            group_results.append(any(results))
        elif group.relational_operator == 'none':
            group_results.append(not any(results))
    if filter_type == 'feed_filter':
        group_relational_operator = processed_feed.feed_group_relational_operator
    elif filter_type == 'summary_filter':
        group_relational_operator = processed_feed.summary_group_relational_operator

    logger.debug(f'  Group results for {filter_type}: {group_results} for {entry.title}')
    if group_relational_operator == 'all':
        return all(group_results)
    elif group_relational_operator == 'any':
        return any(group_results)
    elif group_relational_operator == 'none':
        return not any(group_results)

def match_content(entry, filter, case_sensitive=False):
    content = ''
    if filter.field in ['title', 'title_or_content']:
        content += generate_untitled(entry) + ' '
    if filter.field in ['content', 'title_or_content']:
        try:
            content += entry.content[0].value + ' '
        except:
            pass
        try:
            content += entry.description + ' '
        except:
            pass
    elif filter.field == 'link':
        content = entry.link
    if not content.strip():  # Strip is necessary for removing leading and trailing spaces
        return False

    # Get the filter value for comparison
    filter_value = filter.value
    
    # If not case sensitive, convert both content and filter value to lowercase
    if not case_sensitive:
        content = content.lower()
        filter_value = filter_value.lower()

    if filter.match_type == 'contains':
        return filter_value in content
    elif filter.match_type == 'does_not_contain':
        return filter_value not in content
    elif filter.match_type == 'matches_regex':
        # Add re.IGNORECASE flag if not case sensitive
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(filter_value, content, flags=flags) is not None
    elif filter.match_type == 'does_not_match_regex':
        flags = 0 if case_sensitive else re.IGNORECASE
        return re.search(filter_value, content, flags=flags) is None
    elif filter.match_type == 'shorter_than':
        return len(content) < int(filter_value)
    elif filter.match_type == 'longer_than':
        return len(content) > int(filter_value)

def generate_summary(article, model, output_mode='HTML', prompt=None, other_model=''):
    if model == 'other':
        model = other_model
    if not model or not OPENAI_API_KEY:
        logger.warning('  OpenAI API key or model not set, skipping summary generation')
        return 
    try:
        client_params = {
            "api_key": OPENAI_API_KEY,
            "base_url": OPENAI_BASE_URL
        }
        completion_params = {
            "model": model,
        }
        if OPENAI_PROXY:
            client_params["http_client"] = httpx.Client(proxy=OPENAI_PROXY)

        client = OpenAI(**client_params)
        if output_mode == 'translate':
            messages = [
                {"role": "system", "content": "You are a helpful assistant for translating text."},
                {"role": "user", "content": f"{article.title}"},
                {"role": "assistant", "content": f"{prompt}"},
            ]
            completion_params["messages"] = messages
        elif output_mode == 'json':
            truncated_query = clean_txt_and_truncate(article.content, model, clean_bool=True)
            #additional_prompt = f"Please summarize this article, and output the result only in JSON format. First item of the json is a one-line summary in 15 words named as 'summary_one_line', second item is the 150-word summary named as 'summary_long'. Output result in {language} language."
            messages = [
                {"role": "system", "content": "You are a helpful assistant for processing articles, designed to output JSON format."},
                {"role": "user", "content": f"content: {truncated_query}, title: {article.title}"},
                {"role": "assistant", "content": f"{prompt}"},
            ]
            completion_params["response_format"] = { "type": "json_object" }
            completion_params["messages"] = messages
        elif output_mode == 'HTML':
            truncated_query = clean_txt_and_truncate(article.content, model, clean_bool=False)
            messages = [
                {"role": "system", "content": "You are a helpful assistant for processing article content, designed to output pure and clean HTML format, do not code block the output using triple backticks."},
                {"role": "user", "content": f"{truncated_query}"},
                {"role": "assistant", "content": f"{prompt}"},
            ]
            completion_params["messages"] = messages
        completion = client.chat.completions.create(**completion_params)
        logger.debug(f"prompt is {prompt}")
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f'Failed to generate summary for article {article.title}: {str(e)}')
    
def parse_cron(cron_string):
    parts = cron_string.split()
    if len(parts) != 5:
        raise ValueError("CRON string must have exactly 5 parts separated by spaces (minute, hour, day of month, month, day of week)")
    return {
        'minute': parts[0],
        'hour': parts[1],
        'day': parts[2],
        'month': parts[3],
        'day_of_week': parts[4]
    }
