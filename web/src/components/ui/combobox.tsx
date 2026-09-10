import { Check, ChevronDown } from "lucide-react";
import type * as React from "react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

export type ComboboxOption = {
  value: string;
  label: string;
  group?: string;
};

type ComboboxProps = {
  id?: string;
  name?: string;
  label?: string;
  value?: string;
  options: ComboboxOption[];
  onValueChange: (value: string) => void;
  placeholder?: string;
  hint?: string;
  error?: string;
  disabled?: boolean;
  emptyText?: string;
  className?: string;
  openLabel?: string;
  closeLabel?: string;
};

export function Combobox({
  id,
  name,
  label,
  value = "",
  options,
  onValueChange,
  placeholder = "Choose an option",
  hint,
  error,
  disabled = false,
  emptyText = "No results",
  className = "",
  openLabel,
  closeLabel,
}: ComboboxProps) {
  const { t } = useTranslation();
  const generatedId = useId();
  const baseId = id ?? generatedId;
  const listboxId = `${baseId}-listbox`;
  const hintId = hint && !error ? `${baseId}-hint` : undefined;
  const errorId = error ? `${baseId}-error` : undefined;
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [typing, setTyping] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(0);

  const selected = options.find((option) => option.value === value);
  const displayValue = typing ? query : selected?.label ?? "";
  const filteredOptions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!typing || !normalizedQuery) return options;
    return options.filter((option) => option.label.toLowerCase().includes(normalizedQuery));
  }, [options, query, typing]);

  useEffect(() => {
    if (!open) return;
    const selectedIndex = filteredOptions.findIndex((option) => option.value === value);
    setHighlightedIndex(selectedIndex >= 0 ? selectedIndex : 0);
  }, [open, value]);

  useEffect(() => {
    setHighlightedIndex((current) => Math.min(current, Math.max(filteredOptions.length - 1, 0)));
  }, [filteredOptions.length]);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setTyping(false);
        setQuery("");
      }
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  const close = () => {
    setOpen(false);
    setTyping(false);
    setQuery("");
  };

  const selectOption = (option: ComboboxOption) => {
    onValueChange(option.value);
    close();
    inputRef.current?.focus();
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (disabled) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setHighlightedIndex((current) => Math.min(current + 1, Math.max(filteredOptions.length - 1, 0)));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      setHighlightedIndex((current) => Math.max(current - 1, 0));
      return;
    }
    if (event.key === "Enter" && open) {
      event.preventDefault();
      const option = filteredOptions[highlightedIndex];
      if (option) selectOption(option);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === "Tab") close();
  };

  let lastGroup: string | undefined;

  return (
    <div ref={containerRef} className={`component-field ${className}`}>
      {label ? <label htmlFor={baseId}>{label}</label> : null}
      <div className="component-combobox-wrap">
        <input type="hidden" name={name} value={value} />
        <input
          ref={inputRef}
          id={baseId}
          type="text"
          role="combobox"
          autoComplete="off"
          disabled={disabled}
          value={displayValue}
          placeholder={placeholder}
          aria-expanded={open}
          aria-controls={listboxId}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-activedescendant={open && filteredOptions.length > 0 ? `${baseId}-option-${highlightedIndex}` : undefined}
          aria-describedby={[hintId, errorId].filter(Boolean).join(" ") || undefined}
          aria-invalid={error ? true : undefined}
          className={`component-input component-combobox-input ${error ? "component-input-error" : ""}`}
          onFocus={() => {
            if (!disabled) setOpen(true);
          }}
          onChange={(event) => {
            setQuery(event.target.value);
            setTyping(true);
            setOpen(true);
          }}
          onKeyDown={handleKeyDown}
        />
        <button
          type="button"
          className="component-combobox-toggle"
          tabIndex={-1}
          aria-label={open ? closeLabel ?? t("common.closeOptions") : openLabel ?? t("common.openOptions")}
          disabled={disabled}
          onClick={() => {
            setOpen((current) => !current);
            inputRef.current?.focus();
          }}
        >
          <ChevronDown aria-hidden="true" size={14} className={open ? "component-combobox-toggle-open" : undefined} />
        </button>
        {open ? (
          <div id={listboxId} role="listbox" className="component-combobox-menu" aria-label={label}>
            {filteredOptions.length === 0 ? (
              <p className="component-combobox-empty">{emptyText}</p>
            ) : (
              filteredOptions.map((option, index) => {
                const showGroup = option.group && option.group !== lastGroup;
                lastGroup = option.group;
                return (
                  <div key={option.value}>
                    {showGroup ? <div className="component-combobox-group" role="presentation">{option.group}</div> : null}
                    <button
                      id={`${baseId}-option-${index}`}
                      type="button"
                      role="option"
                      aria-selected={option.value === value}
                      className={`component-combobox-option${index === highlightedIndex ? " component-combobox-option-highlighted" : ""}`}
                      onMouseDown={(event) => event.preventDefault()}
                      onMouseEnter={() => setHighlightedIndex(index)}
                      onClick={() => selectOption(option)}
                    >
                      <span>{option.label}</span>
                      {option.value === value ? <Check aria-hidden="true" size={14} /> : null}
                    </button>
                  </div>
                );
              })
            )}
          </div>
        ) : null}
      </div>
      {error ? <p id={errorId} className="component-field-error">{error}</p> : hint ? <p id={hintId} className="component-field-hint">{hint}</p> : null}
    </div>
  );
}
