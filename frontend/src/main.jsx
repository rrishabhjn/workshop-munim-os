import { Component, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App.jsx";

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-stone-100 flex items-center justify-center p-4">
          <div className="bg-white max-w-md w-full rounded-2xl border border-rose-200 p-6 space-y-3 shadow-sm text-center">
            <div className="text-lg font-extrabold text-rose-900">
              Something went wrong on this screen
            </div>
            <p className="text-xs text-stone-600 break-words">
              {this.state.error?.message || "Unexpected rendering error"}
            </p>
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="min-h-[44px] px-5 rounded-xl bg-amber-900 text-white text-xs font-bold"
            >
              Reload Workshop OS
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>
);
