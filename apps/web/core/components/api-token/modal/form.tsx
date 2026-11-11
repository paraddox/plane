/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import useSWR from "swr";
import { add } from "date-fns";
import { Controller, useForm } from "react-hook-form";
import { Calendar } from "lucide-react";
// types
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { IApiToken } from "@plane/types";
// ui
import { CustomSelect, Input, TextArea, ToggleSwitch } from "@plane/ui";
import { cn, renderFormattedDate, renderFormattedTime } from "@plane/utils";
// components
import { DateDropdown } from "@/components/dropdowns/date";
import { useWorkspace } from "@/hooks/store/use-workspace";
import { ProjectService } from "@/services/project/project.service";
// helpers
type Props = {
  handleClose: () => void;
  neverExpires: boolean;
  toggleNeverExpires: () => void;
  onSubmit: (data: Partial<IApiToken>) => Promise<void>;
};

const EXPIRY_DATE_OPTIONS = [
  {
    key: "1_week",
    label: "1 week",
    value: { weeks: 1 },
  },
  {
    key: "1_month",
    label: "1 month",
    value: { months: 1 },
  },
  {
    key: "3_months",
    label: "3 months",
    value: { months: 3 },
  },
  {
    key: "1_year",
    label: "1 year",
    value: { years: 1 },
  },
];

const defaultValues: Partial<IApiToken> = {
  label: "",
  description: "",
  expired_at: null,
  workspace: null,
  project: null,
  scopes: ["read", "write"],
};

const projectService = new ProjectService();

const getExpiryDate = (val: string): Date | null | undefined => {
  const today = new Date();
  const dateToAdd = EXPIRY_DATE_OPTIONS.find((option) => option.key === val)?.value;
  if (dateToAdd) return add(today, dateToAdd);
  return null;
};

const getFormattedDate = (date: Date): Date => {
  const now = new Date();
  const hours = now.getHours();
  const minutes = now.getMinutes();
  const seconds = now.getSeconds();
  return add(date, { hours, minutes, seconds });
};

export const CreateApiTokenForm = observer(function CreateApiTokenForm(props: Props) {
  const { handleClose, neverExpires, toggleNeverExpires, onSubmit } = props;
  // states
  const [customDate, setCustomDate] = useState<Date | null>(null);
  // form
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    reset,
    setValue,
    watch,
  } = useForm<IApiToken>({ defaultValues });
  // hooks
  const { t } = useTranslation();
  const { workspaces } = useWorkspace();
  const workspaceId = watch("workspace");
  const workspaceSlug = workspaceId ? workspaces[workspaceId]?.slug : undefined;
  const {
    data: projects,
    isLoading: projectsLoading,
    error: projectsError,
  } = useSWR(workspaceSlug ? ["api-token-projects", workspaceSlug] : null, () =>
    workspaceSlug ? projectService.getProjectsLite(workspaceSlug) : Promise.resolve([])
  );

  const handleFormSubmit = async (data: IApiToken) => {
    // if never expires is toggled off, and the user has not selected a custom date or a predefined date, show an error
    if (!neverExpires && (!data.expired_at || (data.expired_at === "custom" && !customDate)))
      return setToast({
        type: TOAST_TYPE.ERROR,
        title: "Error!",
        message: "Please select an expiration date.",
      });

    const payload: Partial<IApiToken> = {
      label: data.label,
      description: data.description,
      workspace: data.workspace,
      project: data.project,
      scopes: data.scopes,
    };

    // if never expires is toggled on, set expired_at to null
    if (neverExpires) payload.expired_at = null;
    // if never expires is toggled off, and the user has selected a custom date, set expired_at to the custom date
    else if (data.expired_at === "custom") {
      payload.expired_at = customDate && getFormattedDate(customDate).toISOString();
    }
    // if never expires is toggled off, and the user has selected a predefined date, set expired_at to the predefined date
    else {
      const expiryDate = getExpiryDate(data.expired_at ?? "");
      if (expiryDate) payload.expired_at = expiryDate.toISOString();
    }

    await onSubmit(payload);
    reset(defaultValues);
    setCustomDate(null);
  };

  const today = new Date();
  const tomorrow = add(today, { days: 1 });
  const expiredAt = watch("expired_at");
  const expiryDate = getExpiryDate(expiredAt ?? "");
  const customDateFormatted = customDate && getFormattedDate(customDate);

  return (
    <form onSubmit={handleSubmit(handleFormSubmit)}>
      <div className="space-y-5 p-5">
        <h3 className="text-18 font-medium text-secondary">
          {t("workspace_settings.settings.api_tokens.create_token")}
        </h3>
        <div className="space-y-3">
          <div className="space-y-1">
            <Controller
              control={control}
              name="label"
              rules={{
                required: t("title_is_required"),
                maxLength: {
                  value: 255,
                  message: t("title_should_be_less_than_255_characters"),
                },
                validate: (val) => val.trim() !== "" || t("title_is_required"),
              }}
              render={({ field: { value, onChange } }) => (
                <Input
                  type="text"
                  value={value}
                  onChange={onChange}
                  hasError={Boolean(errors.label)}
                  placeholder={t("title")}
                  className="w-full text-14"
                />
              )}
            />
            {errors.label && <span className="text-11 text-danger-primary">{errors.label.message}</span>}
          </div>
          <Controller
            control={control}
            name="description"
            render={({ field: { value, onChange } }) => (
              <TextArea
                value={value}
                onChange={onChange}
                hasError={Boolean(errors.description)}
                placeholder={t("description")}
                className="min-h-24 w-full resize-none text-14"
              />
            )}
          />
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <fieldset className="min-w-0 space-y-1">
              <legend className="text-12 text-secondary">Workspace access</legend>
              <Controller
                control={control}
                name="workspace"
                render={({ field: { value, onChange } }) => (
                  <CustomSelect
                    value={value ?? ""}
                    onChange={(selected: string) => {
                      onChange(selected || null);
                      setValue("project", null);
                    }}
                    label={value ? workspaces[value]?.name : "All my workspaces"}
                    buttonClassName="w-full"
                  >
                    <CustomSelect.Option value="">All my workspaces</CustomSelect.Option>
                    {Object.values(workspaces).map((workspace) => (
                      <CustomSelect.Option key={workspace.id} value={workspace.id}>
                        {workspace.name}
                      </CustomSelect.Option>
                    ))}
                  </CustomSelect>
                )}
              />
            </fieldset>
            <fieldset className="min-w-0 space-y-1">
              <legend className="text-12 text-secondary">Project access</legend>
              <Controller
                control={control}
                name="project"
                render={({ field: { value, onChange } }) => (
                  <CustomSelect
                    value={value ?? ""}
                    onChange={(selected: string) => onChange(selected || null)}
                    label={value ? projects?.find((project) => project.id === value)?.name : "All projects"}
                    disabled={!workspaceSlug || projectsLoading}
                    buttonClassName="w-full"
                  >
                    <CustomSelect.Option value="">All projects</CustomSelect.Option>
                    {projects?.map((project) => (
                      <CustomSelect.Option key={project.id} value={project.id}>
                        {project.name}
                      </CustomSelect.Option>
                    ))}
                  </CustomSelect>
                )}
              />
              {projectsError && (
                <span role="alert" className="text-11 text-danger-primary">
                  Could not load projects.
                </span>
              )}
            </fieldset>
          </div>
          <fieldset className="min-w-0 space-y-1">
            <legend className="text-12 text-secondary">Permissions</legend>
            <Controller
              control={control}
              name="scopes"
              render={({ field: { value, onChange } }) => (
                <CustomSelect
                  value={value?.includes("write") ? "write" : "read"}
                  onChange={(selected: string) => onChange(selected === "write" ? ["read", "write"] : ["read"])}
                  label={value?.includes("write") ? "Read and write" : "Read only"}
                  buttonClassName="w-full"
                >
                  <CustomSelect.Option value="write">Read and write</CustomSelect.Option>
                  <CustomSelect.Option value="read">Read only</CustomSelect.Option>
                </CustomSelect>
              )}
            />
          </fieldset>
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <Controller
                control={control}
                name="expired_at"
                render={({ field: { onChange, value } }) => {
                  const selectedOption = EXPIRY_DATE_OPTIONS.find((option) => option.key === value);

                  return (
                    <CustomSelect
                      customButton={
                        <div
                          className={cn(
                            "flex h-7 items-center gap-2 rounded-sm border-[0.5px] border-strong px-2 py-0.5",
                            {
                              "text-placeholder": neverExpires,
                            }
                          )}
                        >
                          <Calendar className="h-3 w-3" />
                          {value === "custom"
                            ? "Custom date"
                            : selectedOption
                              ? selectedOption.label
                              : "Set expiration date"}
                        </div>
                      }
                      value={value}
                      onChange={onChange}
                      disabled={neverExpires}
                    >
                      {EXPIRY_DATE_OPTIONS.map((option) => (
                        <CustomSelect.Option key={option.key} value={option.key}>
                          {option.label}
                        </CustomSelect.Option>
                      ))}
                      <CustomSelect.Option value="custom">Custom</CustomSelect.Option>
                    </CustomSelect>
                  );
                }}
              />
              {expiredAt === "custom" && (
                <div className="h-7">
                  <DateDropdown
                    value={customDate}
                    onChange={(date) => setCustomDate(date)}
                    minDate={tomorrow}
                    icon={<Calendar className="h-3 w-3" />}
                    buttonVariant="border-with-text"
                    placeholder="Set date"
                    disabled={neverExpires}
                  />
                </div>
              )}
            </div>
            {!neverExpires && (
              <span className="text-11 text-placeholder">
                {expiredAt === "custom"
                  ? customDate
                    ? `Expires ${renderFormattedDate(customDateFormatted ?? "")} at ${renderFormattedTime(customDateFormatted ?? "")}`
                    : null
                  : expiredAt
                    ? `Expires ${renderFormattedDate(expiryDate ?? "")} at ${renderFormattedTime(expiryDate ?? "")}`
                    : null}
              </span>
            )}
          </div>
        </div>
      </div>
      <div className="flex items-center justify-between gap-2 border-t-[0.5px] border-subtle px-5 py-4">
        <div className="flex items-center gap-1.5">
          <ToggleSwitch
            value={neverExpires}
            onChange={toggleNeverExpires}
            label={t("workspace_settings.settings.api_tokens.never_expires")}
            size="sm"
          />
          <span className="text-11">{t("workspace_settings.settings.api_tokens.never_expires")}</span>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" onClick={handleClose}>
            {t("cancel")}
          </Button>
          <Button variant="primary" type="submit" loading={isSubmitting}>
            {isSubmitting
              ? t("workspace_settings.settings.api_tokens.generating")
              : t("workspace_settings.settings.api_tokens.generate_token")}
          </Button>
        </div>
      </div>
    </form>
  );
});
