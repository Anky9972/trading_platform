import pandas as pd
import numpy as np

def calculate_volatility_dispersion_scores(data_dict, sector_map):
    """
    Core math for sector-relative volatility dispersion.
    data_dict: {symbol: DataFrame with 'Close'}
    sector_map: {symbol: sector_name}
    """
    vol_data = []
    for symbol, df in data_dict.items():
        if len(df) < 21: continue
        if symbol not in sector_map: continue
        
        # Annualized 20d Volatility
        returns = df['Close'].pct_change().dropna().tail(20)
        vol = returns.std() * np.sqrt(252)
        vol_data.append({"symbol": symbol, "sector": sector_map[symbol], "vol": vol})

    if not vol_data:
        return pd.DataFrame()

    df_vol = pd.DataFrame(vol_data)
    sector_medians = df_vol.groupby('sector')['vol'].transform('median')
    df_vol['dispersion_score'] = df_vol['vol'] / sector_medians
    return df_vol

def calculate_liquidity_imbalance_metrics(df):
    """
    Core math for liquidity imbalance.
    df: DataFrame with 'Close', 'Volume'
    """
    if len(df) < 21:
        return None
        
    p_now = df['Close'].iloc[-1]
    p_prev = df['Close'].iloc[-2]
    p_ret = (p_now - p_prev) / p_prev
    
    vol_now = df['Volume'].iloc[-1]
    vol_sma = df['Volume'].rolling(20).mean().iloc[-1]
    vol_ratio = vol_now / vol_sma if vol_sma > 0 else 1.0
    
    return {
        "price_return": p_ret,
        "volume_ratio": vol_ratio,
        "current_price": p_now
    }
