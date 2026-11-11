export type PayoutRecipientPreview = {
  name: string
  project: string
  amount_cents: number
}

export function payoutPreviewValid(totalCents: number, expectedCount: number,
  recipients: PayoutRecipientPreview[]): boolean
export function payoutConfirmationMessage(totalCents: number,
  recipients: PayoutRecipientPreview[]): string
