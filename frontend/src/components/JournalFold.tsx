import { createContext, useContext } from "react";

/**
 * A Journal section folded to its title line, or open (asked 2026-11-11: "the option to hide or collapse some area").
 * JournalView gives each section its fold; a card puts <FoldButton> before its title and draws only its title line
 * while folded. Outside the Journal there is no fold: the button draws nothing and the card stays open.
 */
export interface Fold {
  folded: boolean;
  toggle: () => void;
}

const FoldContext = createContext<Fold | null>(null);
export const FoldProvider = FoldContext.Provider;
export const useFold = (): Fold | null => useContext(FoldContext);

/** ▾ / ▸ before a card's title: folds the card to that one line, or opens it again. */
export function FoldButton({ name }: { name: string }) {
  const fold = useFold();
  if (!fold) return null;
  return (
    <button
      type="button"
      onClick={fold.toggle}
      aria-expanded={!fold.folded}
      className="-ml-1 w-5 shrink-0 rounded text-neutral-500 hover:bg-neutral-800 hover:text-neutral-200"
      title={fold.folded ? `Open ${name}` : `Fold ${name} to this line`}
    >
      {fold.folded ? "▸" : "▾"}
    </button>
  );
}
