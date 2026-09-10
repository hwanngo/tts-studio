import { ChevronDown } from "lucide-react";
import type * as React from "react";
import { useId } from "react";

type SelectOption = { value: string; label: string };

interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  label?: string;
  options?: SelectOption[];
  hint?: string;
  error?: string;
}

export function Select({ label, options, hint, error, className = "", id, children, ...props }: SelectProps) {
  const generatedId = useId();
  const selectId = id ?? generatedId;
  const hintId = hint && !error ? `${selectId}-hint` : undefined;
  const errorId = error ? `${selectId}-error` : undefined;

  return (
    <div className="component-field">
      {label ? <label htmlFor={selectId}>{label}</label> : null}
      <div className="component-select-wrap">
        <select
          id={selectId}
          aria-describedby={[hintId, errorId].filter(Boolean).join(" ") || undefined}
          aria-invalid={error ? true : undefined}
          className={`component-select ${error ? "component-input-error" : ""} ${className}`}
          {...props}
        >
          {options ? options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>) : children}
        </select>
        <ChevronDown className="component-select-icon" aria-hidden="true" size={14} />
      </div>
      {error ? <p id={errorId} className="component-field-error">{error}</p> : hint ? <p id={hintId} className="component-field-hint">{hint}</p> : null}
    </div>
  );
}
