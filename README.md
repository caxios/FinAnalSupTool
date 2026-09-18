<div align="center">
  <h1>📈 FinAnalSupTool</h1>
  <p><strong>Financial Analysis Support Tool</strong></p>
  <p>FastAPI · React · Gemini API · LlamaIndex · LangChain</p>
</div>

<br />

## 📖 Project Overview

**FinAnalSupTool** is an **AI-powered financial analysis support tool** designed to help users (investors, analysts, etc.) comprehensively analyze a company's financial health, macroeconomic indicators, and market sentiment. By providing financial statements (PDFs), earnings calls, and related YouTube videos, users can leverage AI agents to analyze the data from multiple angles and gain valuable insights through an intuitive chat interface.

## ✨ Key Features

- 💬 **Multi-Agent AI Chatbot**: The system routes your questions to the most appropriate AI agent (Macro Economics, Earnings Calls, SEC Filings, etc.) to generate precise responses.
- 📄 **Document Upload & Analysis (RAG)**: Upload financial statement PDFs to be processed and stored in a vector database (ChromaDB), enabling accurate answers based on the provided documents.
- 📊 **Macroeconomic Data Analysis**: Analyze macroeconomic indicators and see how they relate to the target company.
- 🎥 **Media (YouTube) Transcript Analysis**: Parse, summarize, and analyze transcripts from YouTube videos such as earnings calls or analyst reviews.
- 🔎 **Market Sentiment Analysis**: Evaluate the overall market sentiment for specific stocks or the general market.

## 🛠 Tech Stack

### Backend
- **Framework**: FastAPI
- **Language**: Python 3
- **AI / LLM**: LangChain, LlamaIndex, Google Gemini API
- **Vector Database**: ChromaDB (for local RAG implementation)
- **Key Libraries**: `pdfplumber`, `PyMuPDF` (PDF parsing), `yfinance` (financial data), `youtube-transcript-api` (YouTube transcripts)

### Frontend
- **Framework**: React (Vite)
- **Language**: TypeScript
- **Routing**: React Router v7

## 🚀 Installation & Setup

This project is built for local development and testing. You will need Python and Node.js installed on your machine.

### 1. Clone the Repository
```bash
git clone https://github.com/your-username/FinAnalSupTool.git
cd FinAnalSupTool
```

### 2. Backend Setup
```bash
cd backend
```
#### Install Dependencies
```bash
pip install -r requirements.txt
```
#### Environment Variables
Create a `.env` file in the `backend` directory and add the required API keys:
```env
GEMINI_API_KEY=your_gemini_api_key_here
TAVILY_API_KEY=your_tavily_api_key_here
YOUTUBE_API_KEY=your_youtube_api_key_here
COHERE_API_KEY=your_cohere_api_key_here
```
`COHERE_API_KEY` is optional — it powers cross-encoder reranking of hybrid
search results (`rag/hybrid_search.py`); without it, search still works, just
without the final reranking pass.
#### Run the Server
```bash
uvicorn main:app --reload --port 8000
```
- API documentation will be available at `http://localhost:8000/docs`.

### 3. Frontend Setup
Open a new terminal window and run the following:
```bash
cd frontend
```
#### Install Dependencies
```bash
npm install
# or yarn install
```
#### Run the Development Server
```bash
npm run dev
```
- Access the frontend in your browser at the provided localhost address (typically `http://localhost:5173`).

## 📥 Downloading SEC Filings from the Terminal

Save SEC filings (10-K, 10-Q, 8-K, DEF 14A, …) straight to your machine — no server needed.

```bash
cd backend
python download_filings.py              # interactive: asks for ticker, type, period
```

Or pass everything at once — `TICKER  FILING_TYPE  START  [END]`:

```bash
python download_filings.py AAPL 10-K 2022 2025                # annual reports FY2022–FY2025
python download_filings.py AAPL 10-Q 2024Q1 2025Q2            # quarterly reports
python download_filings.py AAPL 10-K,10-Q 2024 2025           # several types at once
python download_filings.py AAPL 8-K 2026-01-01 2026-09-18     # any other form, by filing date
python download_filings.py NVDA 10-K 2025 --format pdf        # PDF instead of HTML
python download_filings.py AAPL 10-Q 2024 2025 --dry-run      # list only, download nothing
```

- **Any form type works** (`4`, `S-1`, `20-F`, `6-K`, `10-K/A`, `SCHEDULE 13G`, …). Write the name exactly as EDGAR does; quote it if it has a space (`"DEF 14A"`). Since Dec 2024, `SC 13G`/`SC 13D` are named `SCHEDULE 13G`/`SCHEDULE 13D`. Only the primary document is saved, not exhibits.
- **Periods:** 10-K / 10-Q use the company's *fiscal* year (`YYYY` or `YYYYQn`). Other forms use the *filing date* (`YYYY`, `YYYYQn` or `YYYY-MM-DD`). There is no Q4 10-Q; Q4 figures are in the 10-K.
- **Format:** `--format html` (default, fast), `pdf` (headless Chromium, slower), or `both`.
- **Output:** `downloads/{TICKER}/{FORM}/` at the repo root. Change it with `--out`. Existing files are skipped unless `--overwrite`.
- The `downloads/` folder is gitignored, so downloaded filings stay local.

## 📁 Project Structure

```
FinAnalSupTool/
├── backend/                  # FastAPI backend application
│   ├── agents/               # AI Agents (Macro, Earnings, Debate, etc.)
│   ├── parsers/              # Document and data parsing logic
│   ├── providers/            # External data providers (Macro data, etc.)
│   ├── rag/                  # RAG (Retrieval-Augmented Generation) logic
│   ├── routers/              # API endpoint routers (documents, chat, analysis)
│   ├── services/             # Business logic and in-memory storage
│   ├── main.py               # FastAPI application entry point
│   └── requirements.txt      # Python dependencies
├── frontend/                 # React (Vite) frontend application
│   ├── src/                  # Source code and UI components
│   ├── package.json          # Node dependencies
│   └── vite.config.ts        # Vite configuration
└── implementation_plan/      # Project design and implementation plan documents
```

## ⚠️ Disclaimer
- This tool is a **localhost prototype**. Uploaded data and in-memory storage are cleared upon server restart. It is not recommended for production environments.
- The AI-generated analysis provided by this tool does not constitute investment advice or financial recommendations. Please use it for reference purposes only.
