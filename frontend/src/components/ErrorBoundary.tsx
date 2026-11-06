import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Keeps a crash in one part of the page from taking the rest down. Without one, React unmounts the whole
 * tree on an error thrown while rendering: a bug in the trade bar's animation blanked the desk, open trade
 * and all (2026-11-04). The error still goes to the console; `fallback` renders in the part's place.
 */
export class ErrorBoundary extends Component<
  { name: string; fallback: ReactNode | ((error: Error) => ReactNode); children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.name}] crashed - showing its fallback instead`, error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const { fallback } = this.props;
    return typeof fallback === "function" ? fallback(error) : fallback;
  }
}
