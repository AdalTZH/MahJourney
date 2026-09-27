"use client";

/**
 * Providers — root-level client wrappers that must survive page navigation.
 *
 * Mounted in app/layout.tsx (which is never unmounted by the Next.js App
 * Router), so ChatProvider state — message history, open/closed, busy — is
 * preserved when the user navigates between pages. GlobalChatPanel lives here
 * too so the floating button and sliding panel are always in the DOM tree
 * above the page boundary.
 *
 * This component also supplies the `onDirective` callback to ChatProvider.
 * Directives from the backend (navigate, activate_plan, refresh) are handled
 * here because useRouter() must be called inside a client component that is
 * part of the rendered tree — ChatProvider itself is framework-agnostic.
 */

import { useCallback } from "react";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { ChatProvider } from "@/lib/chat-context";
import { GlobalChatPanel } from "@/components/global-chat-panel";
import { NotificationListener } from "@/components/notification-listener";
import { Toaster } from "@/components/ui/toast";
import type { UiDirective } from "@/lib/api";

export function Providers({ children }: { children: ReactNode }) {
  const router = useRouter();

  /**
   * Translate a backend ui_directive into a Next.js navigation call.
   *
   * navigate      → router.push(path) with ?tab= encoded in the URL so the
   *                 target page can read it via useSearchParams() and activate
   *                 the right tab on mount.
   * activate_plan → handled inside chat-context.tsx (calls activatePlan API
   *                 then emits a follow-up navigate directive here).
   * refresh       → router.refresh() asks Next.js to re-fetch server
   *                 components; client state stays intact.
   */
  const handleDirective = useCallback((directive: UiDirective) => {
    if (directive.action === "navigate") {
      const url = directive.tab
        ? `${directive.path}?tab=${directive.tab}`
        : directive.path;
      // Try the client-side router first (preserves chat state via the
      // persistent layout). If for any reason the SPA navigation doesn't take
      // effect in this runtime, fall back to a hard navigation so the
      // dispatcher always ends up on the requested screen.
      try {
        router.push(url);
      } catch {
        if (typeof window !== "undefined") window.location.assign(url);
      }
      // Safety net: if the pathname hasn't changed a tick after the push
      // (some runtimes no-op router.push from a component outside the routed
      // subtree), force a hard navigation.
      if (typeof window !== "undefined") {
        const targetPath = directive.path;
        window.setTimeout(() => {
          if (window.location.pathname !== targetPath) {
            window.location.assign(url);
          }
        }, 150);
      }
    } else if (directive.action === "refresh") {
      router.refresh();
    }
    // "activate_plan" is fully handled in chat-context.tsx before this
    // callback is reached; it never arrives here.
  }, [router]);

  return (
    <ChatProvider onDirective={handleDirective}>
      {children}
      <GlobalChatPanel />
      {/* Live in-app alerts (e.g. driver breakdowns) over the events socket. */}
      <NotificationListener />
      <Toaster />
    </ChatProvider>
  );
}
