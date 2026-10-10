import { describe, expect, it } from "vitest";
import { displayStatus, lastKnownText, statusLabel } from "./status";

describe("displayStatus", () => {
  it("shows the agent status only while the socket feeds snapshots", () => {
    expect(displayStatus(true, "running")).toBe("running");
    expect(displayStatus(true, "paused")).toBe("paused");
    expect(displayStatus(true, "killed")).toBe("killed");
    expect(displayStatus(true, "error")).toBe("error");
  });

  it("never falls back to a stale status when the socket is down", () => {
    expect(displayStatus(false, "paused")).toBe("disconnected");
    expect(displayStatus(false, "running")).toBe("disconnected");
    expect(displayStatus(false, undefined)).toBe("disconnected");
    expect(displayStatus(true, undefined)).toBe("disconnected");
  });
});

describe("statusLabel", () => {
  it("labels every state", () => {
    expect(statusLabel("running")).toBe("Running · paper");
    expect(statusLabel("paused")).toBe("Paused");
    expect(statusLabel("killed")).toBe("Killed");
    expect(statusLabel("error")).toBe("Error");
    expect(statusLabel("disconnected")).toBe("Disconnected");
  });
});

describe("lastKnownText", () => {
  it("shows the stale status and when it last arrived", () => {
    // 2024-10-09T10:30:00Z -> 12:30 Europe/Amsterdam
    const at = Date.UTC(2024, 9, 9, 10, 30, 0);
    expect(lastKnownText("running", at)).toBe("Last known: Running · paper, 12:30");
    expect(lastKnownText("paused", at)).toBe("Last known: Paused, 12:30");
  });
});
