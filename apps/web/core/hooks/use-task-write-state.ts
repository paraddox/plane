import useSWR from "swr";

export function useTaskWriteState(workspaceSlug: string, projectId: string, issueId: string) {
  const { data = 0, mutate } = useSWR<number>(["task-write-state", workspaceSlug, projectId, issueId], null, {
    fallbackData: 0,
  });
  return {
    pending: data > 0,
    begin: () => mutate((count = 0) => count + 1, { revalidate: false }),
    end: () => mutate((count = 1) => count - 1, { revalidate: false }),
  };
}
