import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AppWrapper from "./App.jsx";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <AppWrapper />
  </StrictMode>
);
