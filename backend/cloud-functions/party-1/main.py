import flask
import firebase_admin
from firebase_admin import firestore
from mpc import MPC_Functions
import requests
import math
from nacl.public import SealedBox, PrivateKey

# Application Default credentials are automatically created.
app = firebase_admin.initialize_app()

def calculate_mean(request: flask.Request) -> flask.Response:
    form_data = request.get_json()

    chosen_statistic = form_data.get("statistic").lower()

    if chosen_statistic not in ["gpa", "age", "financial_aid"]:
        return flask.Response(f"Invalid chosen statistics: {chosen_statistic}", status=401)
    
    db = firestore.client(app)

    party1_ref = db.collection("party1")
    docs = party1_ref.stream()

    shares = []

    for doc in docs:
        doc_dict = doc.to_dict()
        shares.append(doc_dict.get(chosen_statistic))

    party1_sum = MPC_Functions.calculate_sum_of_shares(shares)
    party2_response = requests.post("https://us-east1-outstanding-map-421217.cloudfunctions.net/party2_sum", json={"statistic": chosen_statistic})
    party3_response = requests.post("https://us-east1-outstanding-map-421217.cloudfunctions.net/party3_sum", json={"statistic": chosen_statistic})

    if party2_response.status_code != 200 or party3_response.status_code != 200:
        return flask.Response(f"Parties failed computation", status=500)
    
    party2_data = party2_response.json()
    party3_data = party3_response.json()

    sums = [party1_sum, party2_data.get("data"), party3_data.get("data")]

    mean = MPC_Functions.calculate_mean(sums, len(shares))

    return flask.jsonify({"mean": mean})

def calculate_standard_deviation(request: flask.Request) -> flask.Response:
    form_data = request.get_json()

    # Get statistic to calculate sd for
    chosen_statistic = form_data.get("statistic").lower()

    if chosen_statistic not in ["gpa", "age", "financial_aid"]:
        return flask.Response(f"Invalid chosen statistics: {chosen_statistic}", status=401)
    
    url = "https://us-east1-outstanding-map-421217.cloudfunctions.net"

    # Calculate the mean of the chosen statistics
    response = requests.post(f"{url}/calculate_mean", json={"statistic": chosen_statistic})

    if response.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response.reason}", status=500)
    
    mean = int(response.json()["mean"])

    db = firestore.client(app)

    # Get all documents from party 1 and this will set many variables such as number of beaver triples
    party1_ref = db.collection("party1")
    docs = party1_ref.stream()

    shares = []

    for doc in docs:
        doc_dict = doc.to_dict()
        # Gets the shares of the chosen statistic from party 1
        shares.append(doc_dict.get(chosen_statistic))

    # Generate beaver triples for each of the shares we have to calculate squared difference
    response = requests.post(f"{url}/generate_beaver_triples", json={"count": len(shares)})

    if response.status_code != 200:
        return flask.Response(f"Failure when generating beaver triples: ")

    # Get an array of beaver triple objects that each have 3 shares of a, b, c with the last 2 encrypted
    beaver_triples = response.json()

    # Arrays for beaver triples for each of the parties
    beaver_triples_party1 = []
    beaver_triples_party2 = []
    beaver_triples_party3 = []

    # Iterate through each beaver triple in beaver triples which represents one set of multiplication
    for beaver_triple in beaver_triples:
        a_shares = beaver_triple["a_shares"]
        b_shares = beaver_triple["b_shares"]
        c_shares = beaver_triple["c_shares"]

        # Party 1 will get first share from a, b, and c which is unencrypted
        beaver_triples_party1.append({"a_share": a_shares[0], "b_share": b_shares[0], "c_share": c_shares[0]})

        # Party 2 and 3 will get encrypted shares that are encrypted under the public key of the parties
        beaver_triples_party2.append({"a_share": a_shares[1], "b_share": b_shares[1], "c_share": c_shares[1]})
        beaver_triples_party3.append({"a_share": a_shares[2], "b_share": b_shares[2], "c_share": c_shares[2]})

    # Set lists for the masked values that will be generated by this party
    d_shares_p1 = []
    e_shares_p1 = []
    for index, share in enumerate(shares):
        temp_triples = beaver_triples_party1[index]
        d_share, e_share = MPC_Functions.generate_beaver_mask(share - mean, share - mean, temp_triples["a_share"], temp_triples["b_share"])
        d_shares_p1.append(d_share)
        e_shares_p1.append(e_share)

    # Get masked values from party 2 given all the shares of a and b, giving us len(shares) masked pairs of d and e
    party2_results = requests.post(f"{url}/party2_beaver_mask", json={"statistic1": chosen_statistic, "statistic2": chosen_statistic, "a_shares": [share["a_share"] for share in beaver_triples_party2], "b_shares": [share["b_share"] for share in beaver_triples_party2]})
    if party2_results.status_code != 200:
        flask.Response(f"Failed to get second party's masked values", status=500)

     # Get masked values from party 3 given all the shares of a and b, giving us len(shares) masked pairs of d and e
    party3_results = requests.post(f"{url}/party3_beaver_mask", json={"statistic1": chosen_statistic, "statistic2": chosen_statistic, "a_shares": [share["a_share"] for share in beaver_triples_party3], "b_shares": [share["b_share"] for share in beaver_triples_party3]})
    if party3_results.status_code != 200:
        flask.Response(f"Failed to get third party's masked values", status=500)

    party2_masked_values = party2_results.json()
    party3_masked_values = party3_results.json()
    
    # Get all values from json objects
    d_shares_p2 = party2_masked_values.get("d_shares")
    e_shares_p2 = party2_masked_values.get("e_shares")
    d_shares_p3 = party3_masked_values.get("d_shares")
    e_shares_p3 = party3_masked_values.get("e_shares")

    d_shares = []
    e_shares = []

    # Generate a list of lists where each sublist is a full set of d shares 
    # and e shares that add to a d & e pair for a single multiplication
    for i in range(len(d_shares_p1)):
        d_shares.append([d_shares_p1[i], d_shares_p2[i], d_shares_p3[i]])
        e_shares.append([e_shares_p1[i], e_shares_p2[i], e_shares_p3[i]])

    z_shares_p1 = []
    for index, share in enumerate(shares): 
        temp_triples = beaver_triples_party1[index]
        z_share = MPC_Functions.beaver_compute(share - mean, share - mean, temp_triples["c_share"], d_shares[index], e_shares[index], True)

        z_shares_p1.append(z_share)

    print("Z Shares: ", z_shares_p1)
    
    party1_z_sum = MPC_Functions.calculate_sum_of_shares(z_shares_p1)

    party2_results = requests.post(f"{url}/party2_beaver_compute", json={"statistic1": chosen_statistic, "statistic2": chosen_statistic, "c_shares": [share["c_share"] for share in beaver_triples_party2], "d_shares": d_shares, "e_shares": e_shares})
    if party2_results.status_code != 200:
        flask.Response(f"Failed to get second party's computed value", status=500)

    party3_results = requests.post(f"{url}/party3_beaver_compute", json={"statistic1": chosen_statistic, "statistic2": chosen_statistic, "c_shares": [share["c_share"] for share in beaver_triples_party3], "d_shares": d_shares, "e_shares": e_shares})
    if party3_results.status_code != 200:
        flask.Response(f"Failed to get third party's computed values", status=500)

    party2_z_sum = party2_results.json()["data"]
    party3_z_sum = party3_results.json()["data"]

    squared_sum = MPC_Functions.calculate_sum_of_shares([party1_z_sum, party2_z_sum, party3_z_sum])

    standard_deviation = math.sqrt(squared_sum / len(shares)) 

    return flask.jsonify({"sd": standard_deviation})

def calculate_correlation(request: flask.Request) -> flask.Response:
    form_data = request.get_json()

    # Get statistic to calculate sd for
    chosen_statistic_1 = form_data.get("statistic1").lower()
    chosen_statistic_2 = form_data.get("statistic2").lower()

    if chosen_statistic_1 not in ["gpa", "age", "financial_aid"] :
        return flask.Response(f"Invalid chosen statistics: {chosen_statistic_1}", status=401)
    
    if chosen_statistic_2 not in ["gpa", "age", "financial_aid"] :
        return flask.Response(f"Invalid chosen statistics: {chosen_statistic_2}", status=401)
    
    url = "https://us-east1-outstanding-map-421217.cloudfunctions.net"

    # Calculate the mean of the chosen statistics
    response_1 = requests.post(f"{url}/calculate_mean", json={"statistic": chosen_statistic_1})
    response_2 = requests.post(f"{url}/calculate_mean", json={"statistic": chosen_statistic_2})

    if response_1.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_1.reason}", status=500)
    
    if response_2.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_2.reason}", status=500)
    
    mean_1 = int(response_1.json()["mean"])
    mean_2 = int(response_2.json()["mean"])

    db = firestore.client(app)

    # Get all documents from party 1 and this will set many variables such as number of beaver triples
    party1_ref = db.collection("party1")
    docs = party1_ref.stream()

    shares1 = []
    shares2 = []

    for doc in docs:
        doc_dict = doc.to_dict()
        # Gets the shares of the chosen statistic from party 1
        shares1.append(doc_dict.get(chosen_statistic_1))
        shares2.append(doc_dict.get(chosen_statistic_2))


    # Generate beaver triples for each of the shares we have to calculate squared difference
    response = requests.post(f"{url}/generate_beaver_triples", json={"count": len(shares1)})

    if response.status_code != 200:
        return flask.Response(f"Failure when generating beaver triples: ")

    # Get an array of beaver triple objects that each have 3 shares of a, b, c with the last 2 encrypted
    beaver_triples = response.json()

    # Arrays for beaver triples for each of the parties
    beaver_triples_party1 = []
    beaver_triples_party2 = []
    beaver_triples_party3 = []

    # Iterate through each beaver triple in beaver triples which represents one set of multiplication
    for beaver_triple in beaver_triples:
        a_shares = beaver_triple["a_shares"]
        b_shares = beaver_triple["b_shares"]
        c_shares = beaver_triple["c_shares"]

        # Party 1 will get first share from a, b, and c which is unencrypted
        beaver_triples_party1.append({"a_share": a_shares[0], "b_share": b_shares[0], "c_share": c_shares[0]})

        # Party 2 and 3 will get encrypted shares that are encrypted under the public key of the parties
        beaver_triples_party2.append({"a_share": a_shares[1], "b_share": b_shares[1], "c_share": c_shares[1]})
        beaver_triples_party3.append({"a_share": a_shares[2], "b_share": b_shares[2], "c_share": c_shares[2]})

    # Set lists for the masked values that will be generated by this party
    d_shares_p1 = []
    e_shares_p1 = []
    for index in range(len(shares1)):
        temp_triples = beaver_triples_party1[index]
        d_share, e_share = MPC_Functions.generate_beaver_mask(shares1[index], shares2[index], temp_triples["a_share"], temp_triples["b_share"])
        d_shares_p1.append(d_share)
        e_shares_p1.append(e_share)

    # Get masked values from party 2 given all the shares of a and b, giving us len(shares) masked pairs of d and e
    party2_results = requests.post(f"{url}/party2_beaver_mask", json={"statistic1": chosen_statistic_1, "statistic2": chosen_statistic_2, "a_shares": [share["a_share"] for share in beaver_triples_party2], "b_shares": [share["b_share"] for share in beaver_triples_party2]})
    if party2_results.status_code != 200:
        flask.Response(f"Failed to get second party's masked values", status=500)

     # Get masked values from party 3 given all the shares of a and b, giving us len(shares) masked pairs of d and e
    party3_results = requests.post(f"{url}/party3_beaver_mask", json={"statistic1": chosen_statistic_1, "statistic2": chosen_statistic_2, "a_shares": [share["a_share"] for share in beaver_triples_party3], "b_shares": [share["b_share"] for share in beaver_triples_party3]})
    if party3_results.status_code != 200:
        flask.Response(f"Failed to get third party's masked values", status=500)

    party2_masked_values = party2_results.json()
    party3_masked_values = party3_results.json()
    
    # Get all values from json objects
    d_shares_p2 = party2_masked_values.get("d_shares")
    e_shares_p2 = party2_masked_values.get("e_shares")
    d_shares_p3 = party3_masked_values.get("d_shares")
    e_shares_p3 = party3_masked_values.get("e_shares")

    d_shares = []
    e_shares = []

    # Generate a list of lists where each sublist is a full set of d shares 
    # and e shares that add to a d & e pair for a single multiplication
    for i in range(len(d_shares_p1)):
        d_shares.append([d_shares_p1[i], d_shares_p2[i], d_shares_p3[i]])
        e_shares.append([e_shares_p1[i], e_shares_p2[i], e_shares_p3[i]])

    z_shares_p1 = []
    for index in range(len(shares1)): 
        temp_triples = beaver_triples_party1[index]
        z_share = MPC_Functions.beaver_compute(shares1[index], shares2[index], temp_triples["c_share"], d_shares[index], e_shares[index], True)

        z_shares_p1.append(z_share)

    print("Z Shares: ", z_shares_p1)
    
    party1_z_sum = MPC_Functions.calculate_sum_of_shares(z_shares_p1)

    party2_results = requests.post(f"{url}/party2_beaver_compute", json={"statistic1": chosen_statistic_1, "statistic2": chosen_statistic_2, "c_shares": [share["c_share"] for share in beaver_triples_party2], "d_shares": d_shares, "e_shares": e_shares})
    if party2_results.status_code != 200:
        flask.Response(f"Failed to get second party's computed value", status=500)

    party3_results = requests.post(f"{url}/party3_beaver_compute", json={"statistic1": chosen_statistic_1, "statistic2": chosen_statistic_2, "c_shares": [share["c_share"] for share in beaver_triples_party3], "d_shares": d_shares, "e_shares": e_shares})
    if party3_results.status_code != 200:
        flask.Response(f"Failed to get third party's computed values", status=500)

    party2_z_sum = party2_results.json()["data"]
    party3_z_sum = party3_results.json()["data"]

    dot_product = MPC_Functions.calculate_sum_of_shares([party1_z_sum, party2_z_sum, party3_z_sum])
    mean_multiplied = mean_1 * mean_2 * len(shares1)

     # Calculate the mean of the chosen statistics
    response_1 = requests.post(f"{url}/calculate_standard_deviation", json={"statistic": chosen_statistic_1})
    response_2 = requests.post(f"{url}/calculate_standard_deviation", json={"statistic": chosen_statistic_2})

    if response_1.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_1.reason}", status=500)
    
    if response_2.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_2.reason}", status=500)
    
    sd_1 = int(response_1.json()["sd"])
    sd_2 = int(response_2.json()["sd"])

    sd_multiplied = sd_1 * sd_2 * len(shares1)

    # Calculate the mean of the chosen statistics
    response_1 = requests.post(f"{url}/party2_sum", json={"statistic": chosen_statistic_1})
    response_2 = requests.post(f"{url}/party3_sum", json={"statistic": chosen_statistic_1})

    if response_1.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_1.reason}", status=500)
    
    if response_2.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_2.reason}", status=500)
    
    party1_sum_statistic_1 = MPC_Functions.calculate_sum_of_shares(shares1)
    statistic_1_sum = MPC_Functions.calculate_sum_of_shares([int(response_1.json()["data"]), int(response_2.json()["data"]), party1_sum_statistic_1])
    
    response_1 = requests.post(f"{url}/party2_sum", json={"statistic": chosen_statistic_2})
    response_2 = requests.post(f"{url}/party3_sum", json={"statistic": chosen_statistic_2})

    if response_1.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_1.reason}", status=500)
    
    if response_2.status_code != 200:
        return flask.Response(f"Failure when calculating mean: {response_2.reason}", status=500)

    party1_sum_statistic_2 = MPC_Functions.calculate_sum_of_shares(shares2)
    statistic_2_sum = MPC_Functions.calculate_sum_of_shares([int(response_1.json()["data"]), int(response_2.json()["data"]), party1_sum_statistic_2])

    print("X * Y: ", dot_product)
    print("X * (Y Mean): ", statistic_1_sum * mean_2)
    print("Y * (X Mean): ", statistic_2_sum * mean_1)
    print("(X Mean) * (Y Mean): ", mean_multiplied)
    print("(X SD) * (Y SD): ", sd_multiplied)

    correlation = (dot_product - statistic_1_sum * mean_2 - statistic_2_sum * mean_1 + mean_multiplied) / sd_multiplied

    return flask.jsonify({"data": correlation})