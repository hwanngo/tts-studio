import type * as React from "react";
import { forwardRef, useId } from "react";

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  hint?: string;
  error?: string;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, hint, error, className = "", id, ...props },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const hintId = hint && !error ? `${inputId}-hint` : undefined;
  const errorId = error ? `${inputId}-error` : undefined;

  return (
    <div className="component-field">
      {label ? <label htmlFor={inputId}>{label}</label> : null}
      <input
        ref={ref}
        id={inputId}
        aria-describedby={[hintId, errorId].filter(Boolean).join(" ") || undefined}
        aria-invalid={error ? true : undefined}
        className={`component-input ${error ? "component-input-error" : ""} ${className}`}
        {...props}
      />
      {error ? <p id={errorId} className="component-field-error">{error}</p> : hint ? <p id={hintId} className="component-field-hint">{hint}</p> : null}
    </div>
  );
});
