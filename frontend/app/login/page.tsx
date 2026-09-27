"use client";

import { RadioTower } from "lucide-react";
import { useRouter } from "next/navigation";
import { type SyntheticEvent, useEffect, useState } from "react";
import { fetchSessionStatus, login } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: SyntheticEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError("");
    try {
      await login(username, password);
      router.push("/dispatcher");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed.");
    } finally {
      setSubmitting(false);
    }
  };

  // Already logged in (e.g. reopened the tab) — skip straight past the form.
  useEffect(() => {
    void fetchSessionStatus().then((status) => {
      if (status.authenticated) router.push("/dispatcher");
    });
  }, [router]);

  return (
    <div className="login-page">
      <form className="panel login-card" onSubmit={onSubmit}>
        <div className="brand-lockup">
          <div className="brand-mark"><RadioTower size={17} /></div>
          <div><strong>MAHJOURNEY</strong><span>DISPATCH INTELLIGENCE</span></div>
        </div>
        <h1>Admin sign in</h1>
        <label>
          Username
          <input
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error && <p className="login-error" role="alert">{error}</p>}
        <button type="submit" className="primary-action" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
