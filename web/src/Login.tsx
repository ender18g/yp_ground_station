import { useState } from "react";
import type { FC } from "react";
import { AlertTriangle, LogIn } from "lucide-react";

interface LoginProps {
  onLogin: (token: string, username: string) => void;
}

const Login: FC<LoginProps> = ({ onLogin }) => {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || `Login failed: HTTP ${response.status}`);
      }

      const data = await response.json();
      if (data.ok && data.access_token) {
        localStorage.setItem("auth_token", data.access_token);
        localStorage.setItem("username", username);
        onLogin(data.access_token, username);
      } else {
        throw new Error(data.error || "Login failed");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div style={styles.container}>
      <div style={styles.card}>
        <h1 style={styles.title}>TRIDENT YP Ground Station</h1>
        <p style={styles.subtitle}>Sign in to your account</p>

        {error && (
          <div style={styles.errorBox}>
            <AlertTriangle size={20} />
            <span>{error}</span>
          </div>
        )}

        <form onSubmit={handleSubmit} style={styles.form}>
          <div style={styles.formGroup}>
            <label htmlFor="username" style={styles.label}>
              Username
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="admin"
              style={styles.input}
              disabled={isLoading}
              required
            />
          </div>

          <div style={styles.formGroup}>
            <label htmlFor="password" style={styles.label}>
              Password
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              style={styles.input}
              disabled={isLoading}
              required
            />
          </div>

          <button
            type="submit"
            style={{
              ...styles.button,
              opacity: isLoading ? 0.7 : 1,
              cursor: isLoading ? "not-allowed" : "pointer",
            }}
            disabled={isLoading}
          >
            <LogIn size={18} />
            <span>{isLoading ? "Signing in..." : "Sign In"}</span>
          </button>
        </form>

        <p style={styles.defaultCreds}>
          Default credentials: <code>admin</code> / <code>admin</code>
          <br />
          <strong>Change password immediately!</strong>
        </p>
      </div>
    </div>
  );
};

const styles: Record<string, React.CSSProperties> = {
  container: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    minHeight: "100vh",
    background: "linear-gradient(135deg, #1e3a8a 0%, #1f2937 100%)",
    fontFamily: "system-ui, -apple-system, sans-serif",
  },
  card: {
    background: "white",
    borderRadius: "8px",
    boxShadow: "0 20px 25px -5px rgba(0, 0, 0, 0.1)",
    padding: "40px",
    maxWidth: "400px",
    width: "90%",
  },
  title: {
    margin: "0 0 8px 0",
    fontSize: "24px",
    fontWeight: "700",
    color: "#1f2937",
    textAlign: "center",
  },
  subtitle: {
    margin: "0 0 24px 0",
    fontSize: "14px",
    color: "#6b7280",
    textAlign: "center",
  },
  errorBox: {
    display: "flex",
    gap: "12px",
    alignItems: "center",
    padding: "12px",
    marginBottom: "20px",
    background: "#fee2e2",
    border: "1px solid #fca5a5",
    borderRadius: "6px",
    color: "#b91c1c",
    fontSize: "14px",
  },
  form: {
    display: "flex",
    flexDirection: "column",
    gap: "16px",
    marginBottom: "20px",
  },
  formGroup: {
    display: "flex",
    flexDirection: "column",
    gap: "6px",
  },
  label: {
    fontSize: "14px",
    fontWeight: "500",
    color: "#374151",
  },
  input: {
    padding: "10px 12px",
    fontSize: "14px",
    border: "1px solid #d1d5db",
    borderRadius: "6px",
    fontFamily: "inherit",
    transition: "all 200ms",
    boxSizing: "border-box" as const,
  } as React.CSSProperties,
  button: {
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    gap: "8px",
    padding: "10px 16px",
    fontSize: "14px",
    fontWeight: "600",
    background: "#1e40af",
    color: "white",
    border: "none",
    borderRadius: "6px",
    cursor: "pointer",
    transition: "background 200ms",
  } as React.CSSProperties,
  defaultCreds: {
    margin: "0",
    padding: "12px",
    fontSize: "12px",
    color: "#6b7280",
    background: "#f3f4f6",
    borderRadius: "6px",
    lineHeight: "1.5",
    textAlign: "center" as const,
  },
};

export default Login;
