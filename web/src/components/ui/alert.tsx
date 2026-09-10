import type * as React from "react";
import { forwardRef } from "react";
import { AlertCircle, CheckCircle2, Info, TriangleAlert } from "lucide-react";
import { cn } from "@/lib/utils";

type AlertVariant = "info" | "success" | "warning" | "error";

type AlertProps = React.HTMLAttributes<HTMLDivElement> & {
  variant?: AlertVariant;
  title?: string;
  action?: React.ReactNode;
};

const icons = {
  info: Info,
  success: CheckCircle2,
  warning: TriangleAlert,
  error: AlertCircle,
};

export const Alert = forwardRef<HTMLDivElement, AlertProps>(function Alert(
  { variant = "info", title, action, className, children, ...props },
  ref,
) {
  const Icon = icons[variant];
  return (
    <div ref={ref} className={cn("ui-alert", `ui-alert-${variant}`, className)} {...props}>
      <Icon className="ui-alert-icon" aria-hidden="true" size={16} />
      <div className="ui-alert-content">
        {title ? <p className="ui-alert-title">{title}</p> : null}
        <div className="ui-alert-body">{children}</div>
      </div>
      {action ? <div className="ui-alert-action">{action}</div> : null}
    </div>
  );
});
