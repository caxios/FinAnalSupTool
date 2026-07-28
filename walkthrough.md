# Full Application Walkthrough — Financial Analysis Support Tool

Both the **Backend** and **Frontend** are fully implemented. Per your request, all code has been heavily commented to explain every function, variable, and logical step. The architecture was kept clean and straightforward without unnecessary complexity.

---

## 1. Backend (FastAPI + Python)

Located in `c:\Users\mrsim\FinAnalSupTool\backend\`

The backend is built with FastAPI and uses a two-pronged extraction approach (inspired by your `sec_10kq_parser.py` reference):
* **PyMuPDF (`fitz`)** for extracting raw text, paired with flexible regex mapping to identify qualitative sections (MD&A, Footnotes, etc.).
* **pdfplumber** for identifying table boundaries and extracting quantitative data.

### Core Files:
*   [main.py](file:///c:/Users/mrsim/FinAnalSupTool/backend/main.py): The FastAPI application. Defines the 4 endpoints (`/upload`, `/financials`, `/filing-text`, `/periods`), handles file saving, and manages the in-memory data store. Heavily commented to explain the processing pipeline.
*   [pdf_utils.py](file:///c:/Users/mrsim/FinAnalSupTool/backend/pdf_utils.py): The core extraction engine. Contains the form-type aware regex maps (`SECTION_MAP_10K`/`10Q`), the table classification heuristics, and the pandas outer-join logic for merging periods.
*   [schemas.py](file:///c:/Users/mrsim/FinAnalSupTool/backend/schemas.py): Pydantic models defining the API contract.
*   [requirements.txt](file:///c:/Users/mrsim/FinAnalSupTool/backend/requirements.txt): Python dependencies.

---

## 2. Frontend (React + TypeScript)

Located in `c:\Users\mrsim\FinAnalSupTool\frontend\`

The frontend is a fast Vite-based React application. It uses a custom-built, resizable split-screen layout without relying on heavy external UI libraries.

### Core Architecture:
*   [api.ts](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/api.ts) & [types.ts](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/types.ts): Centralized API layer that calls the backend, with TypeScript interfaces mirroring the backend's Pydantic schemas.
*   [App.tsx](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/App.tsx): The root layout. Manages global state (loaded periods) and implements the draggable split-screen divider.
*   [Header.tsx](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/components/Header.tsx) & [UploadModal.tsx](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/components/UploadModal.tsx): The top bar and the drag-and-drop upload overlay.
*   [UpperPane.tsx](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/components/UpperPane.tsx): The quantitative viewer. Renders the merged financial tables with sticky headers and columns.
*   [LowerPane.tsx](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/components/LowerPane.tsx): The qualitative viewer. Lets users select a period and view the extracted text sections (preserving line breaks).
*   [index.css](file:///c:/Users/mrsim/FinAnalSupTool/frontend/src/index.css): The global design system. Implements a dark glassmorphism theme, smooth animations, and layout grids.

---

## 3. How to Run Locally

You need two terminal windows to run both servers simultaneously.

**Terminal 1: Start the Backend**
```powershell
cd c:\Users\mrsim\FinAnalSupTool\backend
# If you haven't activated your virtual environment, do that first.
python -m uvicorn main:app --reload --port 8000
```
*The backend API will run on `http://localhost:8000`. You can view the API docs at `http://localhost:8000/docs`.*

**Terminal 2: Start the Frontend**
```powershell
cd c:\Users\mrsim\FinAnalSupTool\frontend
npm run dev
```
*The React app will usually start on `http://localhost:5173`. Open this URL in your browser.*

---

## 4. Testing the Application

1. Open the frontend in your browser.
2. Click **📁 Upload Files** in the top right.
3. Drag and drop one or more SEC 10-K or 10-Q PDF files.
4. Click **Upload**.
5. Once complete, you will see a status report for each file. Close the modal.
6. The **Upper Pane** will now populate with merged financial tables. You can click the tabs to switch between Balance Sheet, Income Statement, etc.
7. The **Lower Pane** will populate with text. Use the dropdown to select which filing period you want to read, and use the tabs to switch between sections like MD&A.
8. You can **drag the horizontal bar** in the middle of the screen to resize the panes.
