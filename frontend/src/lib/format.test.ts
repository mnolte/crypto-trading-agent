import { describe, expect, it } from "vitest";
import {
  formatChange,
  formatCountdown,
  formatDateTime,
  formatEur,
  formatPrice,
  formatSignedEur,
  formatTime,
} from "./format";

describe("formatEur", () => {
  it("formats a decimal string as euros", () => {
    expect(formatEur("10412.8")).toBe("€10,412.80");
    expect(formatEur("0")).toBe("€0.00");
  });

  it("keeps the minus sign outside the currency", () => {
    expect(formatEur("-12.4")).toBe("−€12.40");
  });

  it("renders an em dash for non-numbers", () => {
    expect(formatEur("not-a-number")).toBe("—");
  });
});

describe("formatPrice", () => {
  it("formats without a currency sign", () => {
    expect(formatPrice("61240.5")).toBe("61,240.50");
  });
});

describe("formatChange / formatSignedEur", () => {
  it("always carries a sign, never colour alone", () => {
    expect(formatChange("1.42")).toBe("+1.42%");
    expect(formatChange("-2.4")).toBe("−2.40%");
    expect(formatChange("0")).toBe("+0.00%");
    expect(formatSignedEur("86.2")).toBe("+€86.20");
    expect(formatSignedEur("-12.4")).toBe("−€12.40");
  });
});

describe("formatTime / formatDateTime", () => {
  it("renders in Europe/Amsterdam", () => {
    // 2024-10-09T10:30:00Z -> 12:30 CEST
    expect(formatTime("2024-10-09T10:30:00Z")).toBe("12:30");
    expect(formatDateTime("2024-10-09T10:30:00Z")).toBe("09 Oct 12:30");
  });

  it("renders an em dash for bad input", () => {
    expect(formatTime("bogus")).toBe("—");
    expect(formatDateTime("bogus")).toBe("—");
  });
});

describe("formatCountdown", () => {
  it("formats seconds as m:ss", () => {
    expect(formatCountdown(701)).toBe("11:41");
    expect(formatCountdown(0)).toBe("0:00");
  });

  it("renders an em dash for negative input", () => {
    expect(formatCountdown(-5)).toBe("—");
  });
});
