import { issueLocalSession } from '../utils/access'
export default defineEventHandler((event) => {
  issueLocalSession(event)
  return { connected: true }
})
