import type * as React from "react";
import { cn } from "@/lib/utils";

type ButtonVariant = "primary" | "neutral" | "dark";

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant | "outline";
  size?: "default" | "small";
  pill?: boolean;
  block?: boolean;
};

const variantClasses: Record<ButtonVariant | "outline", string> = {
  primary: "btn-primary",
  neutral: "btn-neutral",
  dark: "btn-dark",
  outline: "btn-neutral",
};

export function Button({
  className,
  variant = "primary",
  size = "default",
  pill = false,
  block = false,
  type = "button",
  ...props
}: ButtonProps) {
  return (
    <button
      data-slot="button"
      type={type}
      className={cn(
        "btn",
        variantClasses[variant],
        size === "small" && "btn-small",
        pill && "btn-pill",
        block && "btn-block",
        className,
      )}
      {...props}
    />
  );
}
