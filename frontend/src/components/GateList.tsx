import type { components } from "../api/schema";

type Gate = components["schemas"]["Gate"];

const STATUS_COLOR: Record<Gate["status"], string> = {
  pass: "var(--ta-positive)",
  warn: "var(--ta-accent)",
  fail: "var(--ta-negative-text)",
};

/** One row of the Safety gates list: rule name left, mono "value · status" right. */
export function GateRow({ gate }: { gate: Gate }) {
  return (
    <li className="gate-row">
      <span>{gate.name}</span>
      <span className="num" style={{ color: STATUS_COLOR[gate.status] }}>
        {gate.value} · {gate.status}
      </span>
    </li>
  );
}

/** The bordered list of gates as it appears in the decision drawer. */
export function GateList({ gates }: { gates: Gate[] }) {
  return (
    <ul className="gate-list">
      {gates.map((gate) => (
        <GateRow key={gate.name} gate={gate} />
      ))}
    </ul>
  );
}
