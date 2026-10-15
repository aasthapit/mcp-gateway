import { type InputHTMLAttributes, useState } from "react";

export const splitCsv = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

/** Text input for a list: keeps what the user typed (trailing commas too), emits the parsed list. */
export function CsvInput({
  value,
  onList,
  ...rest
}: { value: string[]; onList(next: string[]): void } & Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "value" | "onChange"
>) {
  const [text, setText] = useState(value.join(", "));
  return (
    <input
      {...rest}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onList(splitCsv(e.target.value));
      }}
    />
  );
}
