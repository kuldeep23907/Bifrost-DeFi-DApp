import smartpy as sp

# An Oracle is the on-chain incarnation of an Oracle provider
# It maintains a queue of requests of type request_type containing an amount
# paid, a sender, an entry_point and a list of parameters.

# Parameters of type parameters_type is a list of (name, dict) pairs.

value_type = sp.TVariant(int = sp.TInt, string = sp.TString, bytes = sp.TBytes)

def value_string(s):
    return sp.variant("string", s)

def value_bytes(s):
    return sp.variant("bytes", s)

def value_int(s):
    return sp.variant("int", s)

wLINK_decimals = 1000000000000000000

# Request parameters
parameters_type = sp.TMap(sp.TString, value_type)

# Full request specification type
request_type = sp.TRecord(amount            = sp.TNat,
                          target            = sp.TAddress,
                          job_id            = sp.TBytes,
                          parameters        = parameters_type,
                          timeout           = sp.TTimestamp,
                          client_request_id = sp.TNat)

class Oracle(sp.Contract):
    def __init__(self,
                 admin,
                 token_contract,
                 token_address       = None,
                 min_timeout_minutes = 5,
                 min_amount          = 0):
        self.token_contract = token_contract
        if token_address is None:
            token_address = token_contract.address
        self.init(admin               = admin,
                  active              = True,
                  min_timeout_minutes = min_timeout_minutes,
                  min_amount          = min_amount,
                  token               = token_address,
                  next_id             = 0,
                  requests            = sp.big_map(tkey = sp.TNat, tvalue = request_type.with_fields(client = sp.TAddress)),
                  reverse_requests    = sp.big_map(tkey = sp.TRecord(client = sp.TAddress, client_request_id = sp.TNat), tvalue = sp.TNat)
        )

    @sp.entry_point
    def create_request(self, client, params):
        sp.verify(sp.sender == self.data.token, message = "Invalid source")
        sp.verify(self.data.active, message = "Inactive")
        amount         = params.amount
        target         = params.target
        job_id         = params.job_id
        parameters     = params.parameters
        timeout        = params.timeout
        sp.verify(self.data.min_amount <= amount, message = "Invalid payment")
        sp.verify(sp.now.add_minutes(self.data.min_timeout_minutes) <= timeout, message = "Invalid timeout")
        client_request_id  = params.client_request_id
        new_request = sp.record(client            = client,
                                target            = target,
                                job_id            = job_id,
                                parameters        = parameters,
                                amount            = amount,
                                timeout           = timeout,
                                client_request_id = client_request_id)
        reverse_request_key = sp.compute(sp.record(client = client, client_request_id = client_request_id))
        sp.verify(~self.data.reverse_requests.contains(reverse_request_key), message = "Bad request key")
        self.data.reverse_requests[reverse_request_key] = self.data.next_id
        self.data.requests[self.data.next_id] = new_request
        self.data.next_id += 1

    @sp.entry_point
    def setup(self, active, min_timeout_minutes, min_amount):
        sp.verify(self.data.admin == sp.sender, message = "Privileged operation")
        self.data.active              = active
        self.data.min_timeout_minutes = min_timeout_minutes
        self.data.min_amount          = min_amount

    @sp.entry_point
    def fulfill_request(self, request_id, result):
        # Please note that the target (i.e., the requestor) could refuse to receive the data.
        # We do not want to check active here since sp.sender == admin.
        sp.verify(self.data.admin == sp.sender, message = "Privileged operation")
        request = sp.local('request', self.data.requests[request_id]).value
        token = sp.contract(self.token_contract.batch_transfer.get_type(), self.data.token, entry_point = "transfer").open_some(message = "Incompatible token interface")
        sp.transfer([sp.record(from_ = sp.to_address(sp.self), txs = [sp.record(to_ = self.data.admin, token_id = 0, amount = request.amount)])],
                    sp.tez(0),
                    token)
        target = sp.contract(sp.TRecord(client_request_id = sp.TNat, result = value_type), request.target).open_some(message = "Incompatible client interface")
        sp.transfer(sp.record(client_request_id = request.client_request_id, result = result), sp.mutez(0), target)
        del self.data.requests[request_id]
        reverse_request_key = sp.record(client = request.client, client_request_id = request.client_request_id)
        del self.data.reverse_requests[reverse_request_key]

    @sp.entry_point
    def cancel_request(self, client_request_id):
        # We do not want to check active here (it would be bad for the client).
        # sp.sender needs to be validated; this process is done through the use of the reverse_request_key.
        reverse_request_key = sp.compute(sp.record(client = sp.sender, client_request_id = client_request_id))
        request_id = sp.local('request_id', self.data.reverse_requests[reverse_request_key]).value
        request = sp.local('request', self.data.requests[request_id]).value
        sp.verify(request.timeout <= sp.now, message = "TTL not met")
        token = sp.contract(self.token_contract.batch_transfer.get_type(), self.data.token, entry_point = "transfer").open_some(message = "Incompatible token interface")
        sp.transfer([sp.record(from_ = sp.to_address(sp.self), txs = [sp.record(to_ = request.client, token_id = 0, amount = request.amount)])],
                    sp.tez(0),
                    token)
        del self.data.requests[request_id]
        del self.data.reverse_requests[reverse_request_key]

# A Client is a smart contract that expects to be called by oracles.
# The simplest form of a Client contains a receive entry point (custom
# names are possible) with an arbitrary parameter type and expects to
# be called by the operator of an oracle called oracle_admin.

class Client_requester():
    def request_helper(self, amount, job_id, parameters, oracle, waiting_request_id, target, timeout_minutes = 5):
        parameters = sp.set_type_expr(parameters, parameters_type)
        sp.verify(~ waiting_request_id.is_some(), message = "Request pending")
        target = sp.set_type_expr(target, sp.TContract(sp.TRecord(client_request_id = sp.TNat, result = value_type)))
        waiting_request_id.set(sp.some(self.data.next_request_id))
        token  = sp.contract(sp.TRecord(oracle = sp.TAddress, params = request_type), self.data.token, entry_point = "proxy").open_some(message = "Incompatible token interface")
        params = sp.record(amount        = amount,
                           target        = sp.to_address(target),
                           job_id        = job_id,
                           parameters    = parameters,
                           timeout       = sp.now.add_minutes(timeout_minutes),
                           client_request_id = self.data.next_request_id)
        sp.transfer(sp.record(oracle = oracle, params = params), sp.mutez(0), token)
        self.data.next_request_id += 1

    def cancel_helper(self, oracle, waiting_request_id):
        sp.verify(waiting_request_id.is_some(), message = "No pending request")
        oracle_contract = sp.contract(sp.TNat, oracle, entry_point = "cancel_request").open_some(message = "Incompatible oracle interface")
        sp.transfer(waiting_request_id.open_some(), sp.mutez(0), oracle_contract)
        waiting_request_id.set(sp.none)

class Client_receiver():
    def check_receive(self, oracle, client_request_id, waiting_request_id, result):
        sp.verify(sp.sender == oracle, message = "Invalid source")
        sp.verify(waiting_request_id.is_some() & (waiting_request_id.open_some() == client_request_id), message = "Response mismatch")
        waiting_request_id.set(sp.none)
        sp.set_type(client_request_id, sp.TNat)
        sp.set_type(result, value_type)

    def read_int(self, x):
        return x.open_variant("int")

    def read_bytes(self, x):
        return x.open_variant("bytes")

    def read_string(self, x):
        return x.open_variant("string")

 

class Client(sp.Contract, Client_requester, Client_receiver):
    def __init__(self, token, oracle, fortune_job_id, admin):
        self.init(admin             = admin,
                  oracle            = oracle,
                  token             = token,
                  fortune           = '',
                  next_request_id   = 1,
                  waiting_fortune_id = sp.none,
                  fortune_job_id     = fortune_job_id,
                  totalTokens = 0,
                  depositBalances = sp.big_map(
                    tkey = sp.TString,
                    tvalue = sp.TNat
                  ),
                  withdrawBalances = sp.big_map(
                      tkey = sp.TAddress,
                      tvalue = sp.TInt
                  ),
                  client_requests  = sp.big_map(
                      tkey = sp.TNat,
                      tvalue = sp.TAddress
                  )

                  )

    @sp.entry_point
    def receive_fortune(self, client_request_id, result):
        
        requesting_address = self.data.client_requests[client_request_id]
        
        self.check_receive(self.data.oracle, client_request_id, self.data.waiting_fortune_id, result)
        
        self.data.withdrawBalances[requesting_address] = self.read_int(result)
        

    @sp.entry_point
    def request_fortune(self, params):
        
        self.data.client_requests[self.data.next_request_id] = sp.sender

        self.request_helper(params.payment,   
                            self.data.fortune_job_id,
                            sp.map(l = {"sender": sp.variant("bytes", sp.pack(sp.sender))}), 
                            self.data.oracle, 
                            self.data.waiting_fortune_id, 
                            sp.self_entry_point("receive_fortune"), 
                            params.timeout)


    @sp.entry_point
    def cancel_fortune(self):
        self.cancel_helper(self.data.oracle, self.data.waiting_fortune_id)

    @sp.entry_point
    def change_oracle(self, oracle, fortune_job_id):
        sp.verify(self.data.admin == sp.sender, message = "Privileged operation")
        sp.verify(~ self.data.waiting_fortune_id.is_some(), message = "Request pending")
        self.data.oracle = oracle
        self.data.fortune_job_id = fortune_job_id

    @sp.entry_point
    def deposit(self, params):
        sp.verify(sp.amount > sp.mutez(0), message = "Deposit too low")
        
        contractbal = sp.ediv(sp.balance, sp.tez(1))
        
        sp.if (contractbal.is_some() ):
            bal = sp.fst(contractbal.open_some())
            
            val = sp.split_tokens( sp.amount, self.data.totalTokens, bal)
            
            _natVal = sp.ediv(val, sp.tez(1))
            
        
            sp.if (_natVal.is_some() ):
                natVal = sp.fst(_natVal.open_some())
                
                self.data.withdrawBalances[params] = sp.to_int(natVal)
                self.data.totalTokens += natVal
            
    @sp.entry_point
    def withdraw(self, params): 
        
        bal = self.data.withdrawBalances[sp.sender]
        
        sp.verify(bal != -1, message = "Account already withdrew")
        
        
        #value has increased due to baking
        val = sp.split_tokens( sp.balance, abs(bal), self.data.totalTokens)
        sp.send(sp.sender, val)
        
        
        self.data.withdrawBalances[sp.sender ] = -1
        self.data.totalTokens = abs( self.data.totalTokens - abs(bal) )
        
        
    @sp.entry_point
    def setDelegate(self, params):
        sp.verify(sp.sender == self.data.admin, message = "Privileged operation")

        sp.set_delegate(params.baker)







# Oracle Requests are paid with tokens handled in an FA2 contract.
# The FA2 contract template is extended by a proxy entry point to
# ensure transfers to Oracle and payments are synchronized.

FA2 = sp.import_template("FA2.py")
class Link_token(FA2.FA2):
    @sp.entry_point
    def proxy(self, oracle, params):
        self.transfer.f(self, [sp.record(from_ = sp.sender, txs = [sp.record(to_ = oracle, token_id = 0, amount = params.amount)])])
        oracle_contract = sp.contract(sp.TRecord(client = sp.TAddress, params = request_type),
                                      oracle,
                                      entry_point = "create_request").open_some(message = "Incompatible token interface")
        sp.transfer(sp.record(client = sp.sender, params = params), sp.mutez(0), oracle_contract)

class TokenFaucet(sp.Contract):
    def __init__(self,
                 admin,
                 token_contract,
                 token_address  = None,
                 max_amount     = 10 * wLINK_decimals):
        self.token_contract = token_contract

        if token_address is None:
            token_address = token_contract.address

        self.init(admin               = admin,
                  active              = True,
                  max_amount          = max_amount,
                  token               = token_address)

    @sp.entry_point
    def request_tokens(self, targets):
        sp.set_type(targets, sp.TSet(sp.TAddress))
        token = sp.contract(self.token_contract.batch_transfer.get_type(),
                            self.data.token,
                            entry_point = "transfer").open_some(message = "Incompatible token interface")
        targets = targets.elements().map(lambda target: sp.record(to_ = target, token_id = 0, amount = self.data.max_amount))
        sp.transfer([sp.record(from_ = sp.to_address(sp.self), txs = targets)], sp.tez(0), token)

    @sp.entry_point
    def configure(self, params):
        sp.verify(self.data.admin == sp.sender, message = "Privileged operation")
        self.data.set(params)

# This code was used to originate test contracts and is kept as an example
if False:
    @sp.add_test(name = "Origination")
    def test():
        scenario = sp.test_scenario()

        link_admin_address = sp.address('tz1UBSgsJTTBQWGieRPZmwG3gpAy3kHd7N4y')
        link_token = Link_token(FA2.FA2_config(single_asset = True), link_admin_address)
        scenario += link_token
        link_token_address = sp.address('KT1TQR3eyYCytqBK9EB28J1taa2cX41F9R8x')

        token_faucet = TokenFaucet(link_admin_address, link_token, link_token_address, 10 * wLINK_decimals)
        scenario += token_faucet
        token_faucet_address = sp.address('KT1JWENqDEoGasUty7m22QBPk6gfau8H4VQS')

        oracle1_admin_address = sp.address('tz1fextP23D6Ph2zeGTP8EwkP5Y8TufeFCHA')
        oracle1 = Oracle(oracle1_admin_address, link_token, token_address = link_token_address)
        scenario += oracle1
        oracle1_address = sp.address('KT1VnsKJu8KKnut6qCqig4LVFv3n8wqq6fpy')

        client1_admin_address = sp.address('tz1axGtTkg1hJvGenTrhqpFbW1S8GcQpPdve')
        client1 = Client(link_token_address, oracle1_address, sp.bytes("0x0001"), client1_admin_address)
        scenario += client1
        client1_address = sp.address('KT1K7LyozUeLVv5yu6XeVFwQm2feEGNQXKUN')

if "templates" not in __name__:
    @sp.add_test(name = "Oracle")
    def test():
        scenario = sp.test_scenario()
        scenario.h1("Chainlink Oracles")

        scenario.table_of_contents()

        scenario.h2("Accounts")
        admin = sp.test_account("Administrator")
        
        bob = sp.address("tz1WdHqcAo7gjtFDPMgD6yN8pk1sq67MJRjH")
        
        oracle1 = sp.test_account("Oracle1")

        client1_admin = sp.test_account("Client1 admin")
        client2_admin = sp.test_account("Client2 admin")

        scenario.show([admin, oracle1, client1_admin, client2_admin])

        scenario.h2("Link Token")
        link_token = Link_token(FA2.FA2_config(single_asset = True), admin.address)
        scenario += link_token
        scenario += link_token.mint(address = admin.address,
                                    amount = 200,
                                    symbol = 'tzLINK',
                                    token_id = 0).run(sender = admin)

        scenario.h2("Token Faucet")
        faucet = TokenFaucet(admin.address, link_token, link_token.address, 10)
        scenario += faucet

        scenario.h2("Oracle")
        oracle = Oracle(oracle1.address, link_token)
        scenario += oracle

        scenario.h2("Client1")
        client1 = Client(link_token.address, oracle.address, sp.bytes("0x0001"), client1_admin.address)
        scenario += client1

        scenario.h2("Client2")
        client2 = Client(link_token.address, oracle.address, sp.bytes("0x0001"), client2_admin.address)
        scenario += client2

        scenario.h2("Tokens")
        scenario += link_token.transfer([sp.record(from_ = admin.address, txs = [sp.record(to_ = faucet.address, token_id = 0, amount = 100)])]).run(sender = admin)
        scenario += link_token.transfer([sp.record(from_ = admin.address, txs = [sp.record(to_ = oracle1.address, token_id = 0, amount = 1)])]).run(sender = admin)
        scenario += faucet.request_tokens(sp.set([client1.address, client2.address]))
        #scenario += link_token.transfer([sp.record(from_ = client1_admin.address, txs = [sp.record(to_ = client1.address, token_id = 0, amount = 10)])]).run(sender = client1_admin)

        scenario.h2("Client1 sends a request that gets fulfilled")
        scenario.h3("A request")
        scenario += client1.request_fortune(payment = 1, timeout = 10).run(sender = bob)
