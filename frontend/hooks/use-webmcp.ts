"use client";

import { useEffect, useEffectEvent } from "react";

type ToolDefinition = {
  name: string;
  title: string;
  description: string;
  inputSchema: object;
  annotations: { readOnlyHint: boolean; untrustedContentHint: boolean };
};

type ModelContext = {
  registerTool: (
    tool: ToolDefinition & { execute: (input: unknown) => unknown },
    options?: { signal?: AbortSignal },
  ) => void | Promise<void>;
};

declare global {
  interface Document {
    readonly modelContext?: ModelContext;
  }
}

export function useWebMcpTool(
  definition: ToolDefinition,
  execute: (input: unknown) => unknown,
) {
  const onExecute = useEffectEvent(execute);
  useEffect(() => {
    const context = document.modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    void Promise.resolve(
      context.registerTool(
        { ...definition, execute: (input) => onExecute(input) },
        { signal: lifecycle.signal },
      ),
    ).catch(() => undefined);
    return () => lifecycle.abort();
  }, [definition]);
}
