import os
import faiss
import json
from typing import List, Generator
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_core.documents import Document
from langchain.chains import RetrievalQA
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.prompts import PromptTemplate
from services import json_logger_service
from langchain.callbacks.base import BaseCallbackHandler
import threading
import queue

embedding_model = OpenAIEmbeddings(api_key=os.getenv("OPENAI_API_KEY"))

vectorstore = None


def initialize_vectorstore(logger=None):
    global vectorstore
    dimension = 1536
    faiss_index = faiss.IndexFlatL2(dimension)
    docstore = InMemoryDocstore()
    index_to_docstore_id = {}
    vectorstore = FAISS(embedding_model, faiss_index, docstore, index_to_docstore_id)
    if logger:
        logger.info("[RAG] Initialized empty vectorstore.")


def split_documents(docs: List[Document], logger=None) -> List[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=100)
    chunks = splitter.split_documents(docs)
    if logger:
        logger.info(f"[RAG] Split into {len(chunks)} document chunks.")
    return chunks


def index_articles_from_json(logger=None):
    latest_file = json_logger_service.get_latest_json_file()
    if not latest_file:
        if logger:
            logger.warning("[RAG] No previous JSON report found.")
        return

    if logger:
        logger.info(f"[RAG] Loading vectorstore from: {latest_file}")

    with open(latest_file, "r", encoding="utf-8") as f:
        all_articles = json.load(f)

    docs = []
    for entry in all_articles:
        # Only index articles that are not rejected
        status = entry.get("logging", {}).get("status", "")
        if status == "Rejected":
            continue
        metadata = entry.get("metadata", {})
        content = metadata.get("raw_content", "") or metadata.get("content", "")
        title = metadata.get("title", "")
        source = metadata.get("source", "")
        if content:
            doc = Document(
                page_content=content, metadata={"title": title, "source": source}
            )
            docs.append(doc)

    if docs:
        docs = split_documents(docs, logger=logger)
        vectorstore.add_documents(docs)
        if logger:
            logger.info(f"[RAG] Indexed {len(docs)} chunks into vectorstore.")


class TokenStreamHandler(BaseCallbackHandler):
    def __init__(self):
        self.queue = queue.Queue()
        self.done = threading.Event()

    def on_llm_new_token(self, token: str, **kwargs):
        self.queue.put(token)

    def on_llm_end(self, *args, **kwargs):
        self.done.set()

    def stream(self) -> Generator[str, None, None]:
        while not self.done.is_set() or not self.queue.empty():
            try:
                token = self.queue.get(timeout=0.1)
                yield token
            except queue.Empty:
                continue


def stream_query_articles(
    question: str, top_k: int = 5, logger=None
) -> Generator[str, None, None]:
    global vectorstore
    retriever = vectorstore.as_retriever(search_kwargs={"k": top_k})
    retrieved_docs = retriever.get_relevant_documents(question)

    # Filter out docs whose source is from a rejected article
    # (Assumes only non-rejected docs are indexed, but double check for safety)
    filtered_docs = []
    latest_file = json_logger_service.get_latest_json_file()
    rejected_sources = set()
    if latest_file:
        with open(latest_file, "r", encoding="utf-8") as f:
            all_articles = json.load(f)
            for entry in all_articles:
                status = entry.get("logging", {}).get("status", "")
                if status == "Rejected":
                    metadata = entry.get("metadata", {})
                    source = metadata.get("source", "")
                    rejected_sources.add(source)
    for doc in retrieved_docs:
        url = doc.metadata.get("source", "N/A")
        if url not in rejected_sources:
            filtered_docs.append(doc)

    if logger:
        logger.info(
            f"[RAG] Retrieved {len(filtered_docs)} non-rejected chunks for question: {question}"
        )
        retrieved_sources = set()
        for i, doc in enumerate(filtered_docs, 1):
            source = doc.metadata.get("source", "N/A")
            title = doc.metadata.get("title", "N/A")
            if source not in retrieved_sources:
                logger.info(f"[RAG] Source: {source} | Title: {title}")
                retrieved_sources.add(source)

    # Collect unique sources (title and URL)
    unique_sources = []
    seen = set()
    for doc in filtered_docs:
        title = doc.metadata.get("title", "N/A")
        url = doc.metadata.get("source", "N/A")
        if url and url not in seen:
            unique_sources.append((title, url))
            seen.add(url)

    custom_prompt = PromptTemplate.from_template(
        """
You are an expert technology analyst specializing in emerging trends and innovations across all tech sectors.

Your task is to analyze the provided context and answer the user's question with a comprehensive, well-structured response.

**Guidelines:**
1. **Synthesize Information**: Combine insights from multiple sources when relevant
2. **Focus on Trends**: Identify emerging patterns, breakthroughs, and market shifts
3. **Provide Context**: Explain the significance and implications of developments
4. **Be Actionable**: Include practical insights and recommendations where applicable
5. **Stay Current**: Emphasize recent developments and their future impact
6. **Be Concise**: Keep responses focused and to the point

**Response Structure:**
- Start with a brief overview of the key findings
- Highlight the most significant trends or developments
- Provide context on why these matter
- Include actionable insights or implications
- Mention any notable companies, technologies, or market shifts
- Give one sentences max for each. 
- Use bullets and headings.
**Context:**
{context}

**Question:**
{question}

**Answer:**
"""
    )

    handler = TokenStreamHandler()
    streaming_llm = ChatOpenAI(
        model="gpt-4o-mini",
        temperature=0.2,
        api_key=os.getenv("OPENAI_API_KEY"),
        streaming=True,
        callbacks=[handler],
    )

    qa_chain = RetrievalQA.from_chain_type(
        llm=streaming_llm,
        retriever=retriever,
        return_source_documents=True,
        chain_type_kwargs={"prompt": custom_prompt},
    )

    # Run in a thread so we can yield tokens as they arrive
    def run_chain():
        qa_chain.invoke({"query": question})
        handler.done.set()

    thread = threading.Thread(target=run_chain)
    thread.start()

    for token in handler.stream():
        yield token

    thread.join()

    # After the answer, yield the sources as markdown
    if unique_sources:
        yield "\n\n---\n**Sources:**\n"
        for i, (title, url) in enumerate(unique_sources, 1):
            yield f"- [{title}]({url})\n"
