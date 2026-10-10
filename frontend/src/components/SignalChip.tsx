import type { components } from "../api/schema";
import { Chip } from "./Chip";

type Signal = components["schemas"]["Signal"];

const READ_DIRECTION: Record<Signal["read"], 1 | 0 | -1> = {
  bullish: 1,
  trending: 1,
  bearish: -1,
  neutral: 0,
  ranging: 0,
  low: 0,
  normal: 0,
  high: 0,
};

const CHIP_BY_DIRECTION = { "-1": "down", "0": "neutral", "1": "up" } as const;

/** A compact mono chip for one indicator read; green up, red down, else neutral. */
export function SignalChip({ signal }: { signal: Signal }) {
  const direction = READ_DIRECTION[signal.read];
  return (
    <Chip variant={CHIP_BY_DIRECTION[String(direction) as keyof typeof CHIP_BY_DIRECTION]}>
      {signal.name} {signal.tf}
    </Chip>
  );
}
