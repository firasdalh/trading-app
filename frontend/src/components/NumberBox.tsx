import { useState } from "react";

/**
 * A number box that only hands on a value the backend will take: inside [min, max], and a whole number
 * when the step is. While typing, the text stays as typed - an empty box, or "4." on the way to "4.5" -
 * and turns red until it is such a value; leaving the box shows the value in force again.
 *
 * Why: `Number(e.target.value)` reads an emptied box as 0. On the Strategies board that made a draft the
 * server refused with a 422 on every live check until a number was typed back (2026-11-04).
 */
export function NumberBox({ value, onChange, min, max, step, className, label, title, name }: {
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
  className: string;
  label?: string;   // aria-label, when no <label> wraps the box
  title?: string;
  name?: string;
}) {
  const [text, setText] = useState<string | null>(null);   // null = not being typed in: show the value
  const usable = (v: number) => Number.isFinite(v) && v >= min && v <= max && (step < 1 || Number.isInteger(v));
  const bad = text != null && !usable(text.trim() === "" ? NaN : Number(text));
  return (
    <input
      type="number"
      name={name}
      value={text ?? value}
      min={min}
      max={max}
      step={step}
      aria-label={label}
      title={bad ? `${min} to ${max}${step >= 1 ? ", a whole number" : ""} - not used until it is one` : title}
      onChange={(e) => {
        setText(e.target.value);
        if (usable(e.target.valueAsNumber)) onChange(e.target.valueAsNumber);
      }}
      onBlur={() => setText(null)}
      className={`${className} ${bad ? "!border-bear/70 !text-bear" : ""}`}
    />
  );
}
