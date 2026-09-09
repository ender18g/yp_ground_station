import { useEffect, useState } from "react";
import { KeyRound, RefreshCw, Save, Trash2, UserPlus, Users, X } from "lucide-react";

import {
  createUser,
  deleteUser,
  listUsers,
  updateUserPassword,
  updateUserPermission,
  updateUserPermissions,
  type ManagedUser,
  type PermissionLevel,
} from "./api";

const permissionLevels: Array<{ value: PermissionLevel; label: string }> = [
  { value: "view_only", label: "View only" },
  { value: "waypoint_command", label: "Waypoint command" },
  { value: "mission_planning", label: "Mission planning" },
  { value: "man_overboard", label: "Man overboard" },
  { value: "admin", label: "Admin" },
];

const permissions = [
  ["read_telemetry", "Read telemetry"],
  ["read_vehicle_status", "Read vehicle status"],
  ["send_waypoint", "Send waypoints"],
  ["send_rtb", "Return to base"],
  ["set_vehicle_mode", "Set vehicle mode"],
  ["cancel_sar", "Cancel SAR"],
  ["create_mission", "Create missions"],
  ["upload_mission", "Upload missions"],
  ["search_grid", "Search grids"],
  ["trigger_mob", "Man overboard"],
  ["manage_sitl", "Manage connections"],
  ["manage_settings", "Manage settings"],
  ["manage_video_streams", "Manage video streams"],
  ["manage_users", "Manage users"],
] as const;

function permissionLevelFor(user: ManagedUser): PermissionLevel {
  const permissions = new Set(user.permissions);
  if (permissions.has("manage_users")) return "admin";
  if (permissions.has("trigger_mob")) return "man_overboard";
  if (permissions.has("upload_mission")) return "mission_planning";
  if (permissions.has("send_waypoint")) return "waypoint_command";
  return "view_only";
}

export default function UserManagement({ onClose }: { onClose: () => void }) {
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [permissionLevel, setPermissionLevel] = useState<PermissionLevel>("view_only");
  const [resetPasswords, setResetPasswords] = useState<Record<string, string>>({});
  const [customPermissions, setCustomPermissions] = useState<Record<string, string[]>>({});
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);

  const refreshUsers = async () => {
    setIsLoading(true);
    setError(null);
    try {
      setUsers(await listUsers());
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Unable to load users");
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    void refreshUsers();
  }, []);

  const handleCreate = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIsSaving(true);
    setError(null);
    try {
      await createUser(username.trim(), password, permissionLevel);
      setUsername("");
      setPassword("");
      setPermissionLevel("view_only");
      await refreshUsers();
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Unable to create user");
    } finally {
      setIsSaving(false);
    }
  };

  const handleRoleChange = async (user: ManagedUser, value: PermissionLevel) => {
    setError(null);
    try {
      await updateUserPermission(user.username, value);
      await refreshUsers();
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Unable to update permissions");
    }
  };

  const togglePermission = (usernameToUpdate: string, permission: string, enabled: boolean) => {
    setCustomPermissions((current) => {
      const selected = new Set(current[usernameToUpdate] ?? users.find((user) => user.username === usernameToUpdate)?.permissions ?? []);
      if (enabled) selected.add(permission);
      else selected.delete(permission);
      return { ...current, [usernameToUpdate]: [...selected] };
    });
  };

  const saveCustomPermissions = async (user: ManagedUser) => {
    const selected = customPermissions[user.username] ?? user.permissions;
    setError(null);
    try {
      await updateUserPermissions(user.username, selected);
      setCustomPermissions((current) => {
        const next = { ...current };
        delete next[user.username];
        return next;
      });
      await refreshUsers();
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Unable to update permissions");
    }
  };

  const handlePasswordReset = async (usernameToReset: string) => {
    const newPassword = resetPasswords[usernameToReset]?.trim();
    if (!newPassword) return;
    setError(null);
    try {
      await updateUserPassword(usernameToReset, newPassword);
      setResetPasswords((current) => ({ ...current, [usernameToReset]: "" }));
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Unable to reset password");
    }
  };

  const handleDelete = async (usernameToDelete: string) => {
    if (!confirm(`Delete user '${usernameToDelete}'?`)) return;
    setError(null);
    try {
      await deleteUser(usernameToDelete);
      await refreshUsers();
    } catch (caughtError) {
      setError(caughtError instanceof Error ? caughtError.message : "Unable to delete user");
    }
  };

  return (
    <section className="user-management-panel" aria-label="User management">
      <div className="panel-title">
        <Users size={17} />
        <strong>User Management</strong>
        <button className="icon-button panel-close" title="Close user management" onClick={onClose}>
          <X size={16} />
        </button>
      </div>

      {error && <p className="user-management-error">{error}</p>}

      <form className="user-create-form" onSubmit={handleCreate}>
        <input aria-label="New username" placeholder="Username" value={username} onChange={(event) => setUsername(event.target.value)} required />
        <input aria-label="New user password" placeholder="Password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
        <select aria-label="New user permission level" value={permissionLevel} onChange={(event) => setPermissionLevel(event.target.value as PermissionLevel)}>
          {permissionLevels.map((level) => <option key={level.value} value={level.value}>{level.label}</option>)}
        </select>
        <button className="user-command-button" type="submit" disabled={isSaving} title="Create user">
          <UserPlus size={16} /> Create user
        </button>
      </form>

      <div className="user-management-list-header">
        <strong>Accounts</strong>
        <button className="icon-button" title="Refresh accounts" onClick={() => void refreshUsers()} disabled={isLoading}>
          <RefreshCw size={16} />
        </button>
      </div>

      {isLoading ? (
        <p className="user-management-empty">Loading accounts...</p>
      ) : users.length === 0 ? (
        <p className="user-management-empty">No accounts found.</p>
      ) : (
        <div className="user-management-list">
          {users.map((user) => (
            <div className="user-management-row" key={user.username}>
              <strong>{user.username}</strong>
              <select aria-label={`${user.username} permission level`} value={permissionLevelFor(user)} onChange={(event) => void handleRoleChange(user, event.target.value as PermissionLevel)}>
                {permissionLevels.map((level) => <option key={level.value} value={level.value}>{level.label}</option>)}
              </select>
              <div className="user-password-reset">
                <input
                  aria-label={`New password for ${user.username}`}
                  placeholder="New password"
                  type="password"
                  value={resetPasswords[user.username] ?? ""}
                  onChange={(event) => setResetPasswords((current) => ({ ...current, [user.username]: event.target.value }))}
                />
                <button className="icon-button" title={`Reset ${user.username}'s password`} onClick={() => void handlePasswordReset(user.username)} disabled={!resetPasswords[user.username]?.trim()}>
                  <KeyRound size={15} />
                </button>
              </div>
              <button className="icon-button danger" title={`Delete ${user.username}`} onClick={() => void handleDelete(user.username)}>
                <Trash2 size={15} />
              </button>
              <details className="user-permission-details">
                <summary>Custom permissions</summary>
                <div className="user-permission-list">
                  {permissions.map(([permission, label]) => {
                    const selected = customPermissions[user.username] ?? user.permissions;
                    return (
                      <label key={permission}>
                        <input
                          type="checkbox"
                          checked={selected.includes(permission)}
                          onChange={(event) => togglePermission(user.username, permission, event.target.checked)}
                        />
                        {label}
                      </label>
                    );
                  })}
                </div>
                <button className="user-command-button" onClick={() => void saveCustomPermissions(user)}>
                  <Save size={15} /> Save permissions
                </button>
              </details>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
