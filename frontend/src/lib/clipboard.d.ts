export function copyTextToClipboard(
  text: string,
  environment?: {
    clipboard?: Pick<Clipboard, 'writeText'>
    document?: Document
  },
): Promise<boolean>
