import { Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import type { TAcceptanceCriterion } from "@plane/types";
import { Button } from "@plane/propel/button";

export type TaskIntent = { requirements: string; definition_of_done: TAcceptanceCriterion[] };
type Props = {
  value: TaskIntent;
  onChange: (value: TaskIntent) => void;
  onBlur?: () => void;
  disabled?: boolean;
};
const field =
  "w-full min-w-0 rounded border border-subtle bg-surface-1 px-3 py-2 text-body-sm-regular disabled:opacity-60";

export function TaskIntentFields({ value, onChange, onBlur, disabled = false }: Props) {
  const requirementsId = useId();
  const [blank] = useState(() => ({ id: crypto.randomUUID(), description: "" }));
  const criteria = value.definition_of_done.length ? value.definition_of_done : [blank];
  return (
    <div className="min-w-0 space-y-4" onBlur={onBlur}>
      <div className="space-y-1">
        <label htmlFor={requirementsId} className="text-body-sm-medium">
          Requirements
        </label>
        <textarea
          id={requirementsId}
          className={field}
          rows={3}
          value={value.requirements}
          disabled={disabled}
          onChange={(event) => onChange({ ...value, requirements: event.target.value })}
        />
      </div>
      <fieldset className="min-w-0 space-y-2" disabled={disabled}>
        <legend className="mb-1 text-body-sm-medium">Definition of done</legend>
        {criteria.map((item, index) => (
          <div key={item.id} className="flex min-w-0 items-start gap-2">
            <textarea
              aria-label={`Acceptance criterion ${index + 1}`}
              className={field}
              rows={2}
              value={item.description}
              onChange={(event) =>
                onChange({
                  ...value,
                  definition_of_done: criteria.map((row) =>
                    row.id === item.id ? { ...row, description: event.target.value } : row
                  ),
                })
              }
            />
            <button
              type="button"
              className="grid size-9 shrink-0 place-items-center rounded hover:bg-layer-2"
              title="Remove criterion"
              aria-label={`Remove criterion ${index + 1}`}
              onClick={() =>
                onChange({ ...value, definition_of_done: value.definition_of_done.filter((row) => row.id !== item.id) })
              }
            >
              <Trash2 className="size-4" />
            </button>
          </div>
        ))}
        {!disabled && (
          <Button
            type="button"
            variant="secondary"
            onClick={() =>
              onChange({
                ...value,
                definition_of_done: [...criteria, { id: crypto.randomUUID(), description: "" }],
              })
            }
          >
            <Plus className="size-4" />
            Add criterion
          </Button>
        )}
      </fieldset>
    </div>
  );
}
