import { defineEventHandler, setHeader } from 'h3'
import { assertLdapRequest, assertLocal, authMode, workspaceGate } from '../utils/access'

export default defineEventHandler((event) => {
  setHeader(event, 'Cache-Control', 'no-store')
  if (authMode() === 'ldap') assertLdapRequest(event)
  else assertLocal(event)
  return workspaceGate()
})
