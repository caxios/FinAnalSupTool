/**
 * main.tsx
 * ────────
 * React application entry point.
 *
 * Imports global styles and renders the root App component
 * into the #root element defined in index.html.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>
);
