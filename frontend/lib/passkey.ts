/** WebAuthn (passkeys) in the browser: turn the server's JSON options into what the browser wants and back. */
import { api } from "./api";

type Json = Record<string, unknown>;

function fromB64url(s: string): ArrayBuffer {
  const b64 = s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4);
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function toB64url(buf: ArrayBuffer | null | undefined): string | null {
  if (!buf) return null;
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential && !!navigator.credentials;
}

/** Ask the device for a new passkey, then send it to the server. */
export async function addPasskey(password: string, name: string): Promise<void> {
  const { options, state } = await api.passkeyRegisterOptions(password);
  const o = options as Json & { user: Json; excludeCredentials?: Json[] };
  const publicKey = {
    ...o,
    challenge: fromB64url(o.challenge as string),
    user: { ...o.user, id: fromB64url(o.user.id as string) },
    excludeCredentials: (o.excludeCredentials ?? []).map((c) => ({ ...c, id: fromB64url(c.id as string) })),
  } as unknown as PublicKeyCredentialCreationOptions;
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was created");
  const r = cred.response as AuthenticatorAttestationResponse;
  await api.passkeyRegister(state, {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(r.clientDataJSON),
      attestationObject: toB64url(r.attestationObject),
      transports: typeof r.getTransports === "function" ? r.getTransports() : [],
    },
    clientExtensionResults: cred.getClientExtensionResults(),
    authenticatorAttachment: (cred as PublicKeyCredential & { authenticatorAttachment?: string }).authenticatorAttachment ?? null,
  }, name);
}

/** Sign in with a passkey on this device (or a phone nearby). Returns the user's role. */
export async function signInWithPasskey(): Promise<string | null | undefined> {
  const { options, state } = await api.passkeyLoginOptions();
  const o = options as Json & { allowCredentials?: Json[] };
  const publicKey = {
    ...o,
    challenge: fromB64url(o.challenge as string),
    allowCredentials: (o.allowCredentials ?? []).map((c) => ({ ...c, id: fromB64url(c.id as string) })),
  } as unknown as PublicKeyCredentialRequestOptions;
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null;
  if (!cred) throw new Error("No passkey was chosen");
  const r = cred.response as AuthenticatorAssertionResponse;
  const res = await api.passkeyLogin(state, {
    id: cred.id,
    rawId: toB64url(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: toB64url(r.clientDataJSON),
      authenticatorData: toB64url(r.authenticatorData),
      signature: toB64url(r.signature),
      userHandle: toB64url(r.userHandle),
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  });
  return res.role;
}
