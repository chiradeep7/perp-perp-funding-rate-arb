import ccxt
import concurrent.futures
from typing import Dict, List, Any, TypedDict
from dataclasses import dataclass

# --- Configuration ---
EXCHANGE_IDS: List[str] = [
    'binance', 'bybit', 'kucoin', 'bitget', 'bitmart', 'coinex', 'bingx'
]
QUOTE_CURRENCY: str = 'USDT'

# Funding Rate constraints
MIN_FUNDING_SPREAD_PERCENT: float = 0.1  # Minimum difference in funding rate (e.g., 0.1%)

# Volume Constraints: Focused on MAXIMUM volume for safety (perpetual liquidity)
MIN_VOLUME_USDT: float = 1_000_000.0  # 1 Million USDT minimum per exchange

# Blacklist for specific tokens (e.g., highly volatile or manipulated assets)
BLACKLISTED_TOKENS: List[str] = ['ZKP']

# --- Data Structures ---
class MarketData(TypedDict):
    price: float
    volume: float
    funding_rate: float

@dataclass
class ArbitrageOpportunity:
    symbol: str
    coin: str
    buy_exchange: str  # Where to Long
    sell_exchange: str # Where to Short
    long_funding: float
    short_funding: float
    spread: float
    total_volume: float

class FundingArbitrageScanner:
    def __init__(self, exchange_ids: List[str]):
        self.exchanges: Dict[str, ccxt.Exchange] = {}
        for eid in exchange_ids:
            try:
                exchange_class = getattr(ccxt, eid)
                # CRITICAL: Configure CCXT to target perpetual swap markets by default
                self.exchanges[eid] = exchange_class({
                    'enableRateLimit': True,
                    'options': {'defaultType': 'swap'}
                })
            except AttributeError:
                print(f"Exchange {eid} not supported by CCXT.")

    def fetch_data(self, exchange_id: str) -> Dict[str, Any]:
        """Fetch perpetual swap markets, volumes, and funding rates."""
        ex = self.exchanges[exchange_id]
        result_data: Dict[str, MarketData] = {}
        
        try:
            ex.load_markets()
            
            # Filter for USDT-margined linear perpetual swaps
            symbols = [
                s for s, m in ex.markets.items() 
                if m.get('swap') and m.get('linear') and m.get('settle') == QUOTE_CURRENCY
            ]
            
            if not symbols:
                return {'id': exchange_id, 'error': 'No USDT swap markets found.'}

            tickers = ex.fetch_tickers(symbols)
            
            # Attempt to fetch bulk funding rates. If unsupported, fallback to empty dict.
            try:
                funding_rates = ex.fetch_funding_rates(symbols)
            except Exception:
                funding_rates = {}

            for sym in symbols:
                ticker = tickers.get(sym)
                if not ticker: 
                    continue
                
                # 1. Extract Funding Rate (from bulk endpoint or ticker info fallback)
                fr = funding_rates.get(sym, {}).get('fundingRate')
                if fr is None:
                    info = ticker.get('info', {})
                    fr = info.get('fundingRate') or info.get('lastFundingRate')
                
                if fr is None: 
                    continue
                
                try:
                    fr = float(fr) * 100  # Convert to percentage
                except (ValueError, TypeError):
                    continue
                    
                # 2. Extract Price
                price = ticker.get('last')
                if not price or price <= 0: 
                    continue
                
                # 3. Extract and Validate Volume
                volume = ticker.get('quoteVolume')
                if not volume and ticker.get('baseVolume'):
                    volume = ticker.get('baseVolume') * price
                    
                if volume and volume >= MIN_VOLUME_USDT:
                    result_data[sym] = {
                        'price': price,
                        'volume': volume,
                        'funding_rate': fr
                    }

            return {'id': exchange_id, 'data': result_data}
            
        except Exception as e:
            return {'id': exchange_id, 'error': str(e)}

    def scan(self) -> None:
        print(f"Scanning {len(self.exchanges)} exchanges for Perp-Perp Funding Arbitrage...")
        print(f"Minimum Volume Filter: {MIN_VOLUME_USDT:,.0f} {QUOTE_CURRENCY}")
        
        # Concurrently fetch data from all exchanges
        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = list(executor.map(self.fetch_data, self.exchanges.keys()))

        valid_data = [r for r in results if 'error' not in r]
        opportunities: List[ArbitrageOpportunity] = []

        # Compare every valid exchange pair
        for i in range(len(valid_data)):
            for j in range(i + 1, len(valid_data)):
                ex1 = valid_data[i]
                ex2 = valid_data[j]
                
                common_symbols = set(ex1['data'].keys()) & set(ex2['data'].keys())

                for symbol in common_symbols:
                    coin = symbol.split('/')[0]

                    if coin.upper() in BLACKLISTED_TOKENS:
                        continue

                    d1 = ex1['data'][symbol]
                    d2 = ex2['data'][symbol]
                    
                    fr1 = d1['funding_rate']
                    fr2 = d2['funding_rate']

                    # Spread is the absolute difference between the two funding rates
                    spread = abs(fr1 - fr2)

                    if spread >= MIN_FUNDING_SPREAD_PERCENT:
                        # Determine direction: 
                        # We Short where funding is highest (to receive payment)
                        # We Long where funding is lowest (to pay less or receive payment if negative)
                        if fr1 > fr2:
                            short_ex, short_fr = ex1['id'], fr1
                            long_ex, long_fr = ex2['id'], fr2
                        else:
                            short_ex, short_fr = ex2['id'], fr2
                            long_ex, long_fr = ex1['id'], fr1

                        total_vol = d1['volume'] + d2['volume']

                        opportunities.append(ArbitrageOpportunity(
                            symbol=symbol,
                            coin=coin,
                            buy_exchange=long_ex,
                            sell_exchange=short_ex,
                            long_funding=long_fr,
                            short_funding=short_fr,
                            spread=spread,
                            total_volume=total_vol
                        ))

        if not opportunities:
            print("\nNo Arbitrage Found matching volume and funding spread criteria.")
            return

        # Sort by maximum 24H volume to ensure the deepest liquidity as requested
        sorted_opps = sorted(opportunities, key=lambda x: x.total_volume, reverse=True)

        print(f"\nFound {len(sorted_opps)} Opportunities (Sorted by Maximum 24H Volume):")
        for opp in sorted_opps:
            print("-" * 65)
            print(f"Token: {opp.symbol} | Spread: {opp.spread:.4f}% per interval")
            print(f"Total 24H Volume: {opp.total_volume:,.0f} {QUOTE_CURRENCY}")
            print(f"ACTION -> LONG @ {opp.buy_exchange.upper()}  (Funding: {opp.long_funding:.4f}%)")
            print(f"ACTION -> SHORT @ {opp.sell_exchange.upper()} (Funding: {opp.short_funding:.4f}%)")

if __name__ == "__main__":
    scanner = FundingArbitrageScanner(EXCHANGE_IDS)
    scanner.scan()
