import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
export default defineConfig({plugins:[react()],server:{proxy:{'/v1':{target:'http://127.0.0.1:8765',ws:true}}},test:{environment:'jsdom',setupFiles:['./src/test/setup.ts'],restoreMocks:true}});
