import type * as React from "react";
import { useId } from "react";

interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: string;
  hint?: string;
  error?: string;
}

export function Textarea({ label, hint, error, className = "", id, ...props }: TextareaProps) {
  const generatedId = useId();
  const textareaId = id ?? generatedId;
  const hintId = hint && !error ? `${textareaId}-hint` : undefined;
  const errorId = error ? `${textareaId}-error` : undefined;

  return (
    <div className="component-field">
      {label ? <label htmlFor={textareaId}>{label}</label> : null}
      <textarea
        id={textareaId}
        aria-describedby={[hintId, errorId].filter(Boolean).join(" ") || undefined}
        aria-invalid={error ? true : undefined}
        rows={3}
        className={`component-textarea ${error ? "component-input-error" : ""} ${className}`}
        {...props}
      />
      {error ? <p id={errorId} className="component-field-error">{error}</p> : hint ? <p id={hintId} className="component-field-hint">{hint}</p> : null}
    </div>
  );
}
