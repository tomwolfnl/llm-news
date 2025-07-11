import os
import re
import json
import tiktoken
from pydantic import BaseModel, ValidationError
from services.scraper_service import fetch_article_content
from logger import logger
from langchain_openai import ChatOpenAI
from json_repair import repair_json
from dotenv import load_dotenv

CATEGORIES_FILE = os.path.join(os.path.dirname(__file__), "../categories.json")
with open(CATEGORIES_FILE, "r") as f:
    CATEGORIES_INDEX = json.load(f)["categories"]

OPENAI_MODEL = "gpt-4o-mini"
MAX_TOTAL_TOKENS = 200_000
RESERVED_OUTPUT_TOKENS = 5_000
MAX_INPUT_TOKENS = MAX_TOTAL_TOKENS - RESERVED_OUTPUT_TOKENS

load_dotenv()
llm = ChatOpenAI(model=OPENAI_MODEL, temperature=0.2, api_key=os.getenv("OPENAI_API_KEY"))


class LoggingOutput(BaseModel):
    status: str
    reason: str
    retry: bool
    missing_categories: list[str]


class ResponseOutput(BaseModel):
    categories: list[str]
    insights: list[str]
    summary: str


class ReportOutput(BaseModel):
    response: ResponseOutput
    logging: LoggingOutput


category_list_str = ", ".join(CATEGORIES_INDEX)

system_prompt = f"""
You are a professional tech analyst. Perform these tasks based on the provided content:

1. Set 'logging.status' to "Accepted" or "Rejected" based on the following rules:
- If the content is relevant, informative, and meets the criteria for tech analysis, set logging.status to "Accepted".
- If the content is irrelevant, uninformative, or does not meet the criteria for tech analysis, set logging.status to "Rejected".
- If there are issues, set logging.status to "Error" and set logging.retry to true.
- If the logging.status is "Rejected" or "Error" leave the following fields empty: 'response.categories', 'response.insights', and 'response.summary'.

2. Create an imaginary list 'imaginary_categories' with all the categories that the content relates to.

3. Add categories from 'imaginary_categories' to 'response.categories' only if they are also in the predefined categories list: {category_list_str}.

4. If 'imaginary_categories' contains categories that are not in the predefined list, add them to 'logging.missing_categories'.

5. Set 'response.insights' to short actionable insights based on the content:
- Provide short actionable insights that are specific to the content.
- If no insights can be derived, leave this empty.

6. Set 'response.summary' to a concise summary (max 15000 characters) of the content:
- Summarize the content in a concise manner, focusing on key points, findings, and implications.
- If the content is too short or ambiguous, leave this empty.

7. Set 'logging.reason' to a clear comprehensive chain of reasoning for your decisions:
- If the content is accepted, explain why it is relevant and informative.
- If the content is rejected, explain why it is irrelevant or uninformative.
- If there are issues, explain the nature of the issues.

8. Ensure the output is a valid JSON block with the following structure (never leave out any keys):
{{
    "response": {{
        "categories": [],
        "insights": [],
        "summary": "<summary>"
    }},
    "logging": {{
        "status":"<Accepted|Rejected|Error>",
        "reason": "<reasoning>",
        "retry": <true|false>,
        "missing_categories": []
    }}
}}
"""


def truncate_to_fit(content):
    encoding = tiktoken.encoding_for_model(OPENAI_MODEL)
    while True:
        total_tokens = len(encoding.encode(system_prompt)) + len(
            encoding.encode("Article Content:\n" + content)
        )
        if total_tokens <= MAX_INPUT_TOKENS:
            return content
        logger.warning(
            f"[LLM] Truncating content. Current estimated tokens: {total_tokens}"
        )
        content = content[: int(len(content) * 0.9)]


def extract_json_block(text):
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text.strip()


async def process_article(article):
    retries_left = article.get("retries_left", 1)
    full_content = article.get("content", "")

    while True:
        safe_content = truncate_to_fit(full_content)
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": safe_content},
            ]
            result = await llm.ainvoke(messages)
            raw_json = extract_json_block(result.content)
            repaired = repair_json(raw_json)
            parsed_dict = json.loads(repaired)
            parsed_result = ReportOutput.model_validate(parsed_dict)

            for cat in parsed_result.logging.missing_categories:
                logger.warning(
                    f"[LLM] Found missing category '{cat}' for article: {article['url']}"
                )

            if parsed_result.logging.retry:
                if retries_left > 0:
                    logger.info(
                        f"[LLM] Retry triggered for: {article['url']} (Remaining retries: {retries_left})"
                    )
                    new_method, new_content = await fetch_article_content(
                        logger, article["url"], method="selenium"
                    )
                    if new_content:
                        full_content = new_content
                        article["method"] = "Selenium"
                        retries_left -= 1
                        continue
                    else:
                        logger.error(f"[Scraper] Selenium failed for: {article['url']}")
                else:
                    logger.warning(f"[LLM] No retries left for: {article['url']}")

            logger.info(f"[LLM] {parsed_result.logging.status}: {article['url']}")
            return {
                "logging": parsed_result.logging.model_dump(),
                "response": parsed_result.response.model_dump(),
                "metadata": {
                    "source": article.get("url", ""),
                    "title": article.get("title", ""),
                    "raw_content": full_content,
                    "missing_categories": parsed_result.logging.missing_categories,
                },
            }

        except (json.JSONDecodeError, ValidationError, Exception) as e:
            logger.error(f"[LLM] JSON Parsing or Validation Error: {e}")
            return {
                "logging": {
                    "status": "Error",
                    "reason": str(e),
                    "retry": False,
                    "missing_categories": [],
                },
                "response": {"categories": [], "summary": "", "insights": []},
                "metadata": {
                    "source": article.get("url", ""),
                    "title": article.get("title", ""),
                    "raw_content": full_content,
                    "missing_categories": []
                },
            }

async def validate_article_url(url, title):
    """Use LLM to determine if a URL is an actual article."""
    validation_prompt = f"""
You are a URL validator. Determine if the given URL and title represent an actual article or just a category/section page.

URL: {url}
Title: {title}

Rules for determining if it's an article:
1. It should be a specific piece of content, not a category listing
2. It should have a specific title that describes the content
3. It should not be a general page like "/technology/" or "/news/"
4. It should be a detailed article, story, or report

Respond with a JSON object:
{{
    "is_article": true/false,
    "reason": "brief explanation of your decision"
}}

Examples:
- URL: "https://example.com/technology/2024/06/22/ai-startup-raises-funding" → is_article: true
- URL: "https://example.com/technology/" → is_article: false
- URL: "https://example.com/news/2024/06/22/company-announces-breakthrough" → is_article: true
- URL: "https://example.com/news/" → is_article: false
"""

    try:
        messages = [
            {"role": "system", "content": "You are a URL validator. Respond only with valid JSON."},
            {"role": "user", "content": validation_prompt},
        ]
        result = await llm.ainvoke(messages)
        raw_json = extract_json_block(result.content)
        repaired = repair_json(raw_json)
        parsed_dict = json.loads(repaired)
        
        return parsed_dict.get("is_article", False), parsed_dict.get("reason", "No reason provided")
    except Exception as e:
        logger.error(f"[LLM] URL validation error for {url}: {e}")
        return True, f"Validation failed, defaulting to accept: {str(e)}"
