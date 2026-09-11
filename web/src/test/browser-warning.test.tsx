import {it,expect} from 'vitest';
import {render,screen} from '@testing-library/react';
import {BrowserResourceWarning} from '../components/BrowserView';
it('attributes blocked page resources to ORE and shows host plus reason without URL secrets',()=>{
 render(<BrowserResourceWarning session={{id:'browser',network_diagnostics:{blocked_requests:[{url:'https://challenges.cloudflare.com/api.js?token=private-query',reason:'Embedded origin is outside the mission',resource_type:'script',at:'2026-09-10T00:00:00Z'}],count:1}}}/>);
 expect(screen.getByRole('alert')).toHaveTextContent('ORE blocked a page resource');
 expect(screen.getByRole('alert')).toHaveTextContent('challenges.cloudflare.com');
 expect(screen.getByRole('alert')).toHaveTextContent('Embedded origin is outside the mission');
 expect(screen.getByRole('alert')).not.toHaveTextContent('private-query');
 expect(screen.getByRole('alert')).not.toHaveTextContent('extension');
});
it('does not show a network warning without blocked resources',()=>{render(<BrowserResourceWarning session={{id:'browser',network_diagnostics:{blocked_requests:[],count:0}}}/>);expect(screen.queryByRole('alert')).not.toBeInTheDocument();});
