import type { CurrencyCode, MoneyString } from "./cashclose-types";

const MONEY_INPUT = /^([+-]?)(\d{1,18})(?:\.(\d{1,2}))?$/;

/** Validate and normalize a decimal string to exactly two minor-unit digits. */
export function money(value: string): MoneyString {
  const trimmed = value.trim();
  const match = MONEY_INPUT.exec(trimmed);
  if (!match) {
    throw new TypeError(
      `Invalid money value ${JSON.stringify(value)}; use a plain decimal string with at most two fractional digits.`,
    );
  }

  const [, sign, rawWhole, rawFraction = ""] = match;
  const whole = rawWhole.replace(/^0+(?=\d)/, "");
  const fraction = rawFraction.padEnd(2, "0");
  const normalizedSign = sign === "-" && (whole !== "0" || fraction !== "00") ? "-" : "";
  return `${normalizedSign}${whole}.${fraction}` as MoneyString;
}

export function isMoneyString(value: unknown): value is MoneyString {
  if (typeof value !== "string") return false;
  try {
    return money(value) === value;
  } catch {
    return false;
  }
}

export function moneyToMinorUnits(value: MoneyString | string): bigint {
  const normalized = money(value);
  const negative = normalized.startsWith("-");
  const unsigned = negative ? normalized.slice(1) : normalized;
  const [whole, fraction] = unsigned.split(".") as [string, string];
  const units = BigInt(whole) * BigInt(100) + BigInt(fraction);
  return negative ? -units : units;
}

export function moneyFromMinorUnits(value: bigint): MoneyString {
  const negative = value < BigInt(0);
  const absolute = negative ? -value : value;
  const whole = absolute / BigInt(100);
  const fraction = (absolute % BigInt(100)).toString().padStart(2, "0");
  return `${negative ? "-" : ""}${whole}.${fraction}` as MoneyString;
}

export function addMoney(
  left: MoneyString | string,
  right: MoneyString | string,
): MoneyString {
  return moneyFromMinorUnits(moneyToMinorUnits(left) + moneyToMinorUnits(right));
}

export function subtractMoney(
  left: MoneyString | string,
  right: MoneyString | string,
): MoneyString {
  return moneyFromMinorUnits(moneyToMinorUnits(left) - moneyToMinorUnits(right));
}

export function compareMoney(
  left: MoneyString | string,
  right: MoneyString | string,
): -1 | 0 | 1 {
  const leftMinor = moneyToMinorUnits(left);
  const rightMinor = moneyToMinorUnits(right);
  return leftMinor < rightMinor ? -1 : leftMinor > rightMinor ? 1 : 0;
}

/** Lossless display helper that does not route financial values through Number. */
export function formatMoney(
  value: MoneyString | string,
  currency: CurrencyCode,
): string {
  const normalized = money(value);
  const negative = normalized.startsWith("-");
  const unsigned = negative ? normalized.slice(1) : normalized;
  const [whole, fraction] = unsigned.split(".") as [string, string];
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${currency} ${negative ? "-" : ""}${grouped}.${fraction}`;
}
