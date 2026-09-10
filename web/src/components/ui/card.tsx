import type * as React from "react";
import { cn } from "@/lib/utils";

type CardProps = React.HTMLAttributes<HTMLDivElement> & {
  header?: React.ReactNode;
  footer?: React.ReactNode;
};

export function Card({ header, footer, className, children, ...props }: CardProps) {
  return (
    <div className={cn("ui-card", className)} {...props}>
      {header ? <div className="ui-card-header">{header}</div> : null}
      {children !== null && children !== undefined ? <div className="ui-card-body">{children}</div> : null}
      {footer ? <div className="ui-card-footer">{footer}</div> : null}
    </div>
  );
}
